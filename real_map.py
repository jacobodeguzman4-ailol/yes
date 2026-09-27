"""
real_map.py

Fetches real Philippine province boundaries (ADM1 level) from geoBoundaries
(https://www.geoboundaries.org, CC-BY 4.0 -- an open, actively-maintained
global administrative boundaries database built by William & Mary's geoLab)
and renders an accurate geographic map of NCR + the ~12 surrounding
provinces PAGASA's NCR-PRSD regularly mentions, color-coded by advisory
status (expecting / affecting / affected / not mentioned).

This replaces an earlier attempt that used a free Highcharts-derived
boundary file, which turned out to be missing some provinces PAGASA
mentions constantly (Tarlac, notably). geoBoundaries is a complete,
authoritative dataset, so every Philippine province is present.

Boundaries are fetched once and cached to data/ph_provinces.geojson (which
the GitHub Actions workflow commits back to the repo), so we're not hitting
geoBoundaries' API on every single scheduled run -- only the first time, or
if that cache file is ever deleted.

If this fetch/render ever fails for any reason (network hiccup, API shape
change, etc.), the caller is expected to fall back to the schematic tile-grid
map in render_card.py -- an alert should never fail to send just because the
map couldn't be drawn.
"""

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw

import net_utils

CACHE_PATH = Path(__file__).parent / "data" / "ph_provinces.geojson"
GEOBOUNDARIES_METADATA_URL = "https://www.geoboundaries.org/api/current/gbOpen/PHL/ADM1/"

# The ~13 areas PAGASA's NCR-PRSD regularly names. "Metro Manila" absorbs
# whatever geoBoundaries calls the NCR unit (its exact shapeName varies by
# dataset -- could be "Metropolitan Manila", "National Capital Region", etc.)
RELEVANT_AREAS = [
    "Zambales", "Tarlac", "Nueva Ecija", "Aurora",
    "Bataan", "Pampanga", "Bulacan",
    "Cavite", "Metro Manila", "Rizal",
    "Batangas", "Laguna", "Quezon",
]

COLOR_UNAFFECTED = (222, 226, 230)
COLOR_EXPECTING = (242, 108, 169)         # pink, matches PAGASA's own shade
COLOR_AFFECTING = (75, 24, 120)           # purple, matches PAGASA's own shade
COLOR_AFFECTED_GENERIC = (230, 126, 34)   # orange, for advisories with no EXPECTING/AFFECTING split
COLOR_OUTLINE = (255, 255, 255)
BACKGROUND = (245, 248, 250)


def _normalize_shape_name(name: str) -> str | None:
    """Map a geoBoundaries shapeName to one of our RELEVANT_AREAS, or None."""
    n = name.lower()
    if "manila" in n or "capital region" in n or n.strip() in ("ncr", "national capital region"):
        return "Metro Manila"
    for area in RELEVANT_AREAS:
        if area == "Metro Manila":
            continue
        if area.lower() in n:
            return area
    return None


def _extract_geojson_url(metadata: dict) -> str:
    """
    geoBoundaries' API response includes several download-link fields; field
    naming has changed across API versions, so try the known ones first and
    fall back to scanning every value for something that looks right.
    """
    for key in ("simplifiedGeometryGeoJSON", "gjDownloadURL", "staticDownloadLink"):
        val = metadata.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    for val in metadata.values():
        if isinstance(val, str) and val.startswith("http") and "geojson" in val.lower():
            return val
    raise ValueError(f"Could not find a GeoJSON download URL in geoBoundaries metadata: {metadata!r}")


def fetch_ph_provinces(force_refresh: bool = False) -> dict:
    """Return the ADM1 GeoJSON FeatureCollection for the Philippines, using a local cache."""
    if CACHE_PATH.exists() and not force_refresh:
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("features"):
                return cached
            print("Cached province data has no features; re-fetching.")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Cached province data unreadable ({exc}); re-fetching.")

    resp = net_utils.get(GEOBOUNDARIES_METADATA_URL)
    resp.raise_for_status()
    metadata = resp.json()
    if isinstance(metadata, list):  # API can return a list even for one country+level
        metadata = metadata[0]
    geojson_url = _extract_geojson_url(metadata)

    geo_resp = net_utils.get(geojson_url, timeout=60)
    geo_resp.raise_for_status()
    geojson = geo_resp.json()

    if not geojson.get("features"):
        raise ValueError("geoBoundaries returned no features")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(geojson), encoding="utf-8")
    return geojson


def _shape_name(feature: dict) -> str:
    props = feature.get("properties", {}) or {}
    for key in ("shapeName", "shapeGroup", "NAME_1", "name", "NAME"):
        if props.get(key):
            return str(props[key])
    return ""


def _iter_rings(geometry: dict):
    """Yield each polygon part's exterior ring as a list of [lon, lat] pairs."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        if coords:
            yield coords[0]
    elif gtype == "MultiPolygon":
        for poly in coords:
            if poly:
                yield poly[0]


def render_real_map(area_status: dict, width: int = 820) -> Image.Image:
    """
    Render a real geographic map of NCR + surrounding provinces, colored by
    area_status ({area_name: 'expecting'|'affecting'|'affected'}), the same
    shape produced by render_card.compute_area_status. Raises on any failure
    (network, parsing, no matching features) -- callers should catch and
    fall back to the schematic map rather than let this break an alert.
    """
    geojson = fetch_ph_provinces()

    relevant_features = []
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    for feature in geojson.get("features", []):
        area = _normalize_shape_name(_shape_name(feature))
        if area is None:
            continue
        relevant_features.append((area, feature))
        for ring in _iter_rings(feature.get("geometry", {}) or {}):
            for lon, lat in ring:
                min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
                min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)

    if not relevant_features or min_lon == float("inf"):
        raise ValueError("No relevant provinces matched in geoBoundaries data")

    # small padding around the bounds, in degrees
    pad = 0.3
    min_lon -= pad
    max_lon += pad
    min_lat -= pad
    max_lat += pad

    # Equirectangular projection with a latitude correction -- plenty
    # accurate at the scale of a few degrees (this isn't a national map).
    mean_lat_rad = math.radians((min_lat + max_lat) / 2)
    lon_scale = math.cos(mean_lat_rad)

    geo_w = (max_lon - min_lon) * lon_scale
    geo_h = max_lat - min_lat
    height = max(int(width * geo_h / geo_w), 1)

    img = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(img)

    def project(lon, lat):
        x = (lon - min_lon) * lon_scale / geo_w * width
        y = (max_lat - lat) / geo_h * height
        return (x, y)

    # Draw unaffected/background provinces first, then affected ones on top,
    # so a colored province's border never gets covered by a neighbor drawn later.
    ordered = sorted(relevant_features, key=lambda af: af[0] in area_status)
    for area, feature in ordered:
        status = area_status.get(area)
        color = {
            "expecting": COLOR_EXPECTING,
            "affecting": COLOR_AFFECTING,
            "affected": COLOR_AFFECTED_GENERIC,
        }.get(status, COLOR_UNAFFECTED)

        for ring in _iter_rings(feature.get("geometry", {}) or {}):
            points = [project(lon, lat) for lon, lat in ring]
            if len(points) >= 3:
                draw.polygon(points, fill=color, outline=COLOR_OUTLINE)

    return img
