"""
render_card.py

Renders a PAGASA NCR-PRSD advisory as a PNG image with two parts:
  1. An info card -- colored header, heading, issued time, full body text.
  2. An area map -- NCR + the ~12 surrounding provinces PAGASA's NCR-PRSD
     regularly names, color-coded by whether each is EXPECTING conditions,
     currently AFFECTING, or (for advisories that don't split into those two
     sections, like Thunderstorm Watch) generically "affected".

The map is a REAL geographic map (accurate province boundaries, fetched from
the open geoBoundaries dataset -- see real_map.py) whenever that fetch
succeeds. If it ever fails for any reason (network hiccup, API changes),
this falls back to a simplified schematic tile-grid instead, so a bug in the
map-fetching code never prevents an alert from being sent -- it just sends
with a plainer map that run.
"""

import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path(__file__).parent / "assets"
FONT_REGULAR = ASSETS_DIR / "fonts" / "DejaVuSans.ttf"
FONT_BOLD = ASSETS_DIR / "fonts" / "DejaVuSans-Bold.ttf"

CARD_WIDTH = 900
PADDING = 40
HEADER_HEIGHT = 130
MAP_WIDTH = CARD_WIDTH - 2 * PADDING  # both the real map and the schematic fallback render at this width

# RGB colors per advisory type (matches the Discord embed colors)
CARD_COLORS = {
    "thunderstorm watch": (241, 196, 15),        # yellow
    "thunderstorm advisory": (230, 126, 34),      # orange
    "thunderstorm information": (149, 165, 166),  # grey
    "rainfall advisory": (52, 152, 219),          # blue
    "heavy rainfall warning": (231, 76, 60),      # red
}
DEFAULT_COLOR = (46, 204, 113)  # green fallback

COLOR_UNAFFECTED = (222, 226, 230)
COLOR_EXPECTING = (242, 108, 169)         # pink, matches PAGASA's "expecting" shade
COLOR_AFFECTING = (75, 24, 120)           # purple, matches PAGASA's "affecting" shade
COLOR_AFFECTED_GENERIC = (230, 126, 34)   # orange, used when there's no EXPECTING/AFFECTING split

# The ~13 areas PAGASA's NCR-PRSD regularly names.
AREAS = [
    "Zambales", "Tarlac", "Nueva Ecija", "Aurora",
    "Bataan", "Pampanga", "Bulacan",
    "Cavite", "Metro Manila", "Rizal",
    "Batangas", "Laguna", "Quezon",
]

# NCR cities that should count as a "Metro Manila" mention
_NCR_CITIES = [
    "Caloocan", "Las Piñas", "Las Pinas", "Makati", "Malabon", "Mandaluyong",
    "Manila", "Marikina", "Muntinlupa", "Navotas", "Parañaque", "Paranaque",
    "Pasay", "Pasig", "Pateros", "Quezon City", "San Juan", "Taguig", "Valenzuela",
]
_NCR_GENERIC = ["Metro Manila", "Greater Metro Manila Area", "National Capital Region", "NCR"]


def _area_pattern(name: str) -> str:
    if name == "Quezon":
        # Avoid matching the "Quezon" inside "Quezon City" (that's NCR, not
        # Quezon province)
        return r"\bQuezon\b(?!\s+City)"
    if name == "Manila":
        return r"(?<!Metro )(?<!Greater Metro )\bManila\b"
    return r"\b" + re.escape(name) + r"\b"


def _text_mentions_area(text: str, area: str) -> bool:
    if area == "Metro Manila":
        return any(
            re.search(_area_pattern(c), text, flags=re.IGNORECASE) for c in _NCR_CITIES
        ) or any(
            re.search(r"\b" + re.escape(g) + r"\b", text, flags=re.IGNORECASE) for g in _NCR_GENERIC
        )
    return bool(re.search(_area_pattern(area), text, flags=re.IGNORECASE))


def compute_area_status(body: str) -> dict[str, str]:
    """
    Return {area_name: status} for every AREAS entry mentioned in body, where
    status is 'expecting', 'affecting', or 'affected' (generic, used when the
    advisory doesn't split into EXPECTING/AFFECTING sections).
    """
    status: dict[str, str] = {}

    m_exp = re.search(r"EXPECTING\s*:(.*?)(?=\bAFFECTING\s*:|\Z)", body, re.IGNORECASE | re.DOTALL)
    m_aff = re.search(r"AFFECTING\s*:(.*?)\Z", body, re.IGNORECASE | re.DOTALL)

    if m_exp or m_aff:
        if m_exp:
            for area in AREAS:
                if _text_mentions_area(m_exp.group(1), area):
                    status[area] = "expecting"
        if m_aff:
            for area in AREAS:
                if _text_mentions_area(m_aff.group(1), area):
                    status[area] = "affecting"  # overrides 'expecting' if both
    else:
        for area in AREAS:
            if _text_mentions_area(body, area):
                status[area] = "affected"

    return status


@lru_cache(maxsize=None)
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _draw_legend(draw: ImageDraw.ImageDraw, top_left: tuple[int, int], width: int) -> int:
    """Draw a horizontal legend row. Returns its height."""
    x0, y0 = top_left
    font_legend = _font(FONT_REGULAR, 15)
    items = [
        (COLOR_EXPECTING, "Expecting"),
        (COLOR_AFFECTING, "Currently affecting"),
        (COLOR_AFFECTED_GENERIC, "Affected (see text)"),
        (COLOR_UNAFFECTED, "Not mentioned"),
    ]
    x = x0
    for color, label in items:
        draw.rounded_rectangle([x, y0, x + 18, y0 + 18], radius=4, fill=color)
        draw.text((x + 26, y0 + 1), label, font=font_legend, fill=(60, 60, 60))
        x += 26 + draw.textlength(label, font=font_legend) + 24
    return 24


def _render_schematic_map(area_status: dict[str, str], width: int) -> Image.Image:
    """Fallback map: a simple tile grid, correct relative position but not to scale."""
    grid = {
        "Zambales":     (0, 0), "Tarlac":  (0, 1), "Nueva Ecija": (0, 2), "Aurora": (0, 3),
        "Bataan":       (1, 0), "Pampanga": (1, 1), "Bulacan":    (1, 2),
        "Cavite":       (2, 0), "Metro Manila": (2, 1), "Rizal":  (2, 2),
        "Batangas":     (3, 0), "Laguna":  (3, 1), "Quezon":      (3, 2),
    }
    cols, rows = 4, 4
    gap = 10
    tile_w = (width - (cols - 1) * gap) // cols
    tile_h = 75

    grid_height = rows * tile_h + (rows - 1) * gap
    legend_height = 30
    caption_height = 24
    total_h = grid_height + legend_height + caption_height

    img = Image.new("RGB", (width, total_h), "white")
    draw = ImageDraw.Draw(img)
    font_tile = _font(FONT_BOLD, 14)

    for area, (row, col) in grid.items():
        status = area_status.get(area)
        color = {
            "expecting": COLOR_EXPECTING,
            "affecting": COLOR_AFFECTING,
            "affected": COLOR_AFFECTED_GENERIC,
        }.get(status, COLOR_UNAFFECTED)
        text_color = "white" if status else (80, 80, 80)

        tx = col * (tile_w + gap)
        ty = row * (tile_h + gap)
        draw.rounded_rectangle([tx, ty, tx + tile_w, ty + tile_h], radius=8, fill=color)

        words = area.split(" ")
        lines, current = [], ""
        for w in words:
            trial = f"{current} {w}".strip()
            if draw.textlength(trial, font=font_tile) <= tile_w - 12:
                current = trial
            else:
                lines.append(current)
                current = w
        if current:
            lines.append(current)

        text_h = len(lines) * 18
        ly = ty + (tile_h - text_h) // 2
        for line in lines:
            lw = draw.textlength(line, font=font_tile)
            draw.text((tx + (tile_w - lw) / 2, ly), line, font=font_tile, fill=text_color)
            ly += 18

    _draw_legend(draw, (0, grid_height + 8), width)
    draw.text(
        (0, grid_height + legend_height + 4),
        "Schematic layout \u2014 real map unavailable this run (network hiccup); relative position only",
        font=_font(FONT_REGULAR, 14),
        fill=(150, 150, 150),
    )
    return img


def get_map_image(area_status: dict[str, str], width: int = MAP_WIDTH) -> tuple[Image.Image, str]:
    """
    Return (image, kind) for the area map: a real geographic map when the
    geoBoundaries fetch succeeds, else the schematic tile-grid fallback.
    """
    try:
        from real_map import render_real_map
        map_img = render_real_map(area_status, width=width)

        # Append a legend + caption strip below the real map so both map
        # types have the same overall shape (image + explanatory footer).
        legend_h, caption_h = 30, 40
        composite = Image.new("RGB", (width, map_img.height + legend_h + caption_h), "white")
        composite.paste(map_img, (0, 0))
        draw = ImageDraw.Draw(composite)
        _draw_legend(draw, (0, map_img.height + 6), width)
        draw.text(
            (0, map_img.height + legend_h + 8),
            "Real province boundaries \u2014 source: geoBoundaries.org (CC-BY 4.0)",
            font=_font(FONT_REGULAR, 14),
            fill=(150, 150, 150),
        )
        return composite, "real"
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: never let map rendering block an alert
        print(f"Real map unavailable ({exc}); using schematic fallback.")
        return _render_schematic_map(area_status, width), "schematic"


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap text to fit max_width, respecting existing newlines."""
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def render_advisory_card(advisory: dict) -> bytes:
    """
    Render one advisory dict (as produced by watch_pagasa.extract_advisories)
    into a PNG image (info card + area map). Returns raw PNG bytes.
    """
    color = CARD_COLORS.get(advisory["type"], DEFAULT_COLOR)

    font_eyebrow = _font(FONT_REGULAR, 20)
    font_heading = _font(FONT_BOLD, 32)
    font_issued = _font(FONT_REGULAR, 20)
    font_body = _font(FONT_REGULAR, 22)
    font_section = _font(FONT_BOLD, 20)

    area_status = compute_area_status(advisory["body"])
    map_img, _map_kind = get_map_image(area_status, width=MAP_WIDTH)

    measurer = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    body_lines = _wrap_text(measurer, advisory["body"], font_body, CARD_WIDTH - 2 * PADDING)
    heading_lines = _wrap_text(measurer, advisory["heading"], font_heading, CARD_WIDTH - 2 * PADDING)

    line_height = 30
    body_block_height = max(len(body_lines), 1) * line_height
    heading_block_height = max(len(heading_lines), 1) * 40

    total_height = (
        HEADER_HEIGHT
        + heading_block_height
        + 50   # issued-at line + spacing
        + 20   # divider spacing
        + body_block_height
        + 30   # spacing before map section
        + 35   # "Affected areas" section title
        + map_img.height
        + PADDING * 2
        + 40   # footer
    )
    total_height = max(total_height, 400)

    img = Image.new("RGB", (CARD_WIDTH, int(total_height)), "white")
    draw = ImageDraw.Draw(img)

    # Header band
    draw.rectangle([0, 0, CARD_WIDTH, HEADER_HEIGHT], fill=color)
    draw.text((PADDING, 20), "DOST-PAGASA", font=font_eyebrow, fill="white")
    draw.text((PADDING, 45), "National Capital Region \u2013 PAGASA Regional Services Division", font=font_eyebrow, fill="white")
    draw.text((PADDING, 80), "Regional Weather Forecasting Center", font=font_eyebrow, fill="white")

    y = HEADER_HEIGHT + 30

    # Heading
    for line in heading_lines:
        draw.text((PADDING, y), line, font=font_heading, fill=(30, 30, 30))
        y += 40

    y += 5
    draw.text((PADDING, y), f"Issued at: {advisory['issued_at'].replace('Issued at:', '').strip()}", font=font_issued, fill=(90, 90, 90))
    y += 40

    # Divider
    draw.line([(PADDING, y), (CARD_WIDTH - PADDING, y)], fill=(220, 220, 220), width=2)
    y += 25

    # Body (the "WORDS")
    for line in body_lines:
        draw.text((PADDING, y), line, font=font_body, fill=(40, 40, 40))
        y += line_height

    y += 30

    # Area map (the "MAP") -- real geographic map, or schematic fallback
    draw.text((PADDING, y), "Affected areas", font=font_section, fill=(30, 30, 30))
    y += 35
    img.paste(map_img, (PADDING, y))
    y += map_img.height

    # Footer
    footer_y = int(total_height) - 35
    draw.line([(PADDING, footer_y - 10), (CARD_WIDTH - PADDING, footer_y - 10)], fill=(230, 230, 230), width=1)
    draw.text(
        (PADDING, footer_y),
        "Source: pagasa.dost.gov.ph/regional-forecast/ncrprsd",
        font=_font(FONT_REGULAR, 16),
        fill=(150, 150, 150),
    )

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
