#!/usr/bin/env python3
"""
watch_pagasa.py

Watches DOST-PAGASA's official NCR-PRSD regional forecast page for new
Rainfall Advisory / Heavy Rainfall Warning / Thunderstorm Advisory / Thunderstorm
Watch bulletins, and posts new ones to a Discord webhook.

Why this page and not Facebook directly:
  PAGASA's own site publishes the exact same bulletin text that gets posted to
  Facebook, at the same time (see https://pagasa.dost.gov.ph/learnings/legend,
  which states these are "disseminated via SMS, Social Media, and website").
  It's a stable, official, scrape-friendly source, unlike Facebook, which
  actively blocks automated access.

Source page: https://pagasa.dost.gov.ph/regional-forecast/ncrprsd

State (which advisories we've already alerted on) is kept in state.json so
this script is safe to run on a schedule without spamming duplicate alerts.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import net_utils
from render_card import render_advisory_card

SOURCE_URL = "https://pagasa.dost.gov.ph/regional-forecast/ncrprsd"
STATE_PATH = Path(__file__).parent / "state.json"

# How many previously-seen advisory keys to keep in state.json (bounds file size)
MAX_SEEN_KEYS = 300

# Lines are classified as an advisory heading if they contain one of these
# keywords (case-insensitive) AND mention "PRSD" (the NCR_PRSD hashtag PAGASA
# tags every regional bulletin with). This is deliberately loose about exact
# punctuation/color-coding (e.g. "Heavy Rainfall Warning (Orange)") so small
# wording changes on PAGASA's site don't silently break detection.
KEYWORDS = [
    "rainfall advisory",
    "heavy rainfall warning",
    "thunderstorm advisory",
    "thunderstorm watch",
    "thunderstorm information",
]

# Once a heading line is hit, we collect the following lines as the body of
# that advisory until we see the next heading, or one of these section
# markers that indicate we've left the advisory feed entirely.
STOP_MARKERS = [
    "SPECIAL FORECAST",
    "IMPACT BASED FORECAST",
    "GREATER METRO MANILA AREA FORECAST",
    "RADAR MOSAIC",
    "SATELLITE LAYERS",
    "ACTIVE WARNINGS",
    "LATEST WEATHER",
]

# Lines like "As of today, there is no Heavy Rainfall Warning Issued." contain
# a KEYWORDS phrase but are NOT an actual bulletin -- they're PAGASA saying
# nothing is currently in effect. Anything matching these stays excluded even
# though it contains a keyword.
NEGATION_PATTERNS = [
    "there is no",
    "no rainfall advisory",
    "no heavy rainfall warning",
    "no thunderstorm",
]

# How many lines ahead of a heading we're willing to look for a lone
# "#NCR_PRSD" tag fragment or the "Issued at:" line, in case PAGASA's markup
# puts them in a separate text node than the heading itself (this was the
# actual cause of a total-silence bug: requiring the tag on the *same* line
# as the heading meant zero advisories were ever detected).
LOOKAHEAD = 3

# All 17 NCR local government units, plus the broader labels PAGASA uses.
# Used to call out specifically *which* parts of NCR an advisory names,
# instead of a generic "Metro Manila" catch-all.
NCR_AREAS = [
    "Caloocan",
    "Las Piñas",
    "Las Pinas",
    "Makati",
    "Malabon",
    "Mandaluyong",
    "Manila",
    "Marikina",
    "Muntinlupa",
    "Navotas",
    "Parañaque",
    "Paranaque",
    "Pasay",
    "Pasig",
    "Pateros",
    "Quezon City",
    "San Juan",
    "Taguig",
    "Valenzuela",
]
NCR_GENERIC_LABELS = ["Metro Manila", "Greater Metro Manila Area", "National Capital Region", "NCR"]

DISCORD_COLORS = {
    "thunderstorm watch": 0xF1C40F,       # yellow
    "thunderstorm advisory": 0xE67E22,    # orange
    "thunderstorm information": 0x95A5A6, # grey
    "rainfall advisory": 0x3498DB,        # blue
    "heavy rainfall warning": 0xE74C3C,   # red
}


def fetch_page(url: str) -> str:
    resp = net_utils.get(url)
    resp.raise_for_status()
    return resp.text


def classify(heading_line: str) -> str:
    """Return which KEYWORDS entry matched a heading line (for coloring/labeling)."""
    ll = heading_line.lower()
    for kw in KEYWORDS:
        if kw in ll:
            return kw
    return "advisory"


def is_negation(line: str) -> bool:
    ll = line.lower()
    return any(neg in ll for neg in NEGATION_PATTERNS)


def is_heading(line: str) -> bool:
    """
    A line is a bulletin heading if it contains one of KEYWORDS and isn't a
    negation statement ("no Heavy Rainfall Warning Issued"). Deliberately does
    NOT require the "#NCR_PRSD" tag on the same line -- PAGASA's markup can
    put that tag in its own text node, which lands on a separate line once
    the page is flattened to plain text.
    """
    if is_negation(line):
        return False
    ll = line.lower()
    return any(kw in ll for kw in KEYWORDS)


def is_tag_fragment(line: str) -> bool:
    """A short standalone line that's just the '#NCR_PRSD' hashtag (or similar)."""
    return len(line) <= 20 and ("prsd" in line.lower() or line.startswith("#"))


def is_stop_marker(line: str) -> bool:
    up = line.upper()
    return any(marker in up for marker in STOP_MARKERS)


def find_ncr_areas(advisory_text: str) -> list[str]:
    """Return the specific NCR cities named in the advisory text, in the order listed."""
    found = []
    for area in NCR_AREAS:
        if area == "Manila":
            # Don't count "Manila" as a specific-city mention when it's only
            # appearing as part of the generic phrase "(Greater) Metro Manila".
            pattern = r"(?<!Metro )(?<!Greater Metro )\bManila\b"
        else:
            pattern = r"\b" + re.escape(area) + r"\b"
        if re.search(pattern, advisory_text, flags=re.IGNORECASE):
            # Normalize the two spelling variants we track for the same city
            canonical = {"Las Pinas": "Las Piñas", "Paranaque": "Parañaque"}.get(area, area)
            if canonical not in found:
                found.append(canonical)
    return found


def mentions_ncr_generically(advisory_text: str) -> bool:
    return any(
        re.search(r"\b" + re.escape(label) + r"\b", advisory_text, flags=re.IGNORECASE)
        for label in NCR_GENERIC_LABELS
    )


def extract_advisories(html: str) -> list[dict]:
    """
    Parse the page and return a list of advisory dicts:
      {heading, issued_at, body, type, key}
    in the order they appear on the page (newest first, matching PAGASA's layout).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Collapse to a clean list of non-empty, stripped lines. This makes the
    # parser resilient to whatever exact HTML tags/classes PAGASA's CMS uses,
    # since we only care about the text content and its order.
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n") if ln.strip()]

    advisories = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if is_heading(line):
            heading = line
            issued_at = ""
            body_lines = []

            # Look ahead a few lines for a lone hashtag fragment (fold it into
            # the heading) and/or the "Issued at:" line, in case they're split
            # onto their own lines by PAGASA's markup.
            j = i + 1
            lookahead_end = min(j + LOOKAHEAD, n)
            while j < lookahead_end and is_tag_fragment(lines[j]):
                heading = f"{heading} {lines[j]}".strip()
                j += 1
                lookahead_end = min(j + LOOKAHEAD, n)

            if j < n and lines[j].lower().startswith("issued at"):
                issued_at = lines[j]
                j += 1

            # Collect body until next heading or a stop marker
            while j < n and not is_heading(lines[j]) and not is_stop_marker(lines[j]):
                body_lines.append(lines[j])
                j += 1

            body = "\n".join(body_lines).strip()
            adv_type = classify(heading)
            key_source = f"{heading}|{issued_at}"
            key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()

            advisories.append(
                {
                    "heading": heading,
                    "issued_at": issued_at,
                    "body": body,
                    "type": adv_type,
                    "key": key,
                }
            )

            i = j
        else:
            i += 1

    return advisories


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"initialized": False, "seen": []}


def save_state(state: dict) -> None:
    # Bound the size of the seen list
    state["seen"] = state["seen"][-MAX_SEEN_KEYS:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def mentions_metro_manila_or_qc(advisory: dict) -> bool:
    """True if the advisory text names any NCR city or a generic NCR/Metro Manila label."""
    text = f"{advisory['heading']} {advisory['body']}"
    return bool(find_ncr_areas(text)) or mentions_ncr_generically(text)


def send_discord(webhook_url: str, advisory: dict) -> None:
    color = DISCORD_COLORS.get(advisory["type"], 0x2ECC71)
    body = advisory["body"]
    # Discord embed description limit is 4096 chars; leave headroom
    if len(body) > 3500:
        body = body[:3500] + "\n… (truncated — see full bulletin on PAGASA's site)"

    filename = f"advisory_{advisory['key'][:8]}.png"
    payload = {
        "embeds": [
            {
                "title": advisory["heading"],
                "description": f"**{advisory['issued_at']}**\n\n{body}",
                "color": color,
                "url": SOURCE_URL,
                "image": {"url": f"attachment://{filename}"},
                "footer": {"text": "Source: DOST-PAGASA NCR-PRSD regional forecast page"},
            }
        ]
    }

    full_text = f"{advisory['heading']} {advisory['body']}"
    named_areas = find_ncr_areas(full_text)
    if named_areas:
        areas_str = ", ".join(named_areas)
        payload["content"] = f"\U0001F6A8 **NCR AREAS AFFECTED: {areas_str}**"
    elif mentions_ncr_generically(full_text):
        payload["content"] = "\U0001F6A8 **METRO MANILA / NCR may be affected**"

    image_bytes = render_advisory_card(advisory)
    resp = requests.post(
        webhook_url,
        data={"payload_json": json.dumps(payload)},
        files={"file": (filename, image_bytes, "image/png")},
        timeout=30,
    )
    resp.raise_for_status()


def build_test_advisory() -> dict:
    """A guaranteed, clearly-labeled sample advisory for --test mode."""
    return {
        "heading": "\U0001F9EA TEST MESSAGE \u2014 Thunderstorm Advisory No. 0 #NCR_PRSD",
        "issued_at": "Issued at: this is a test send, not a real bulletin",
        "body": (
            "This is a TEST message from your PAGASA NCR-PRSD advisory watcher, sent to confirm "
            "the Discord webhook and image rendering are working end-to-end.\n\n"
            "Sample content \u2014 EXPECTING: rains over portions of Caloocan, Marikina and "
            "Quezon City. This is not a real advisory."
        ),
        "type": "thunderstorm advisory",
        "key": "test-message-not-real",
    }


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        return 1

    if "--test" in sys.argv:
        # Prefer sending whatever's actually live on PAGASA's page right now
        # (real data is the best test); fall back to a synthetic sample if
        # nothing is currently posted. Either way, state.json is untouched.
        try:
            html = fetch_page(SOURCE_URL)
            live_advisories = extract_advisories(html)
        except requests.RequestException as exc:
            print(f"Could not fetch the live page ({exc}); using a synthetic sample instead.")
            live_advisories = []

        if live_advisories:
            adv = dict(live_advisories[0])
            adv["heading"] = f"\U0001F9EA TEST SEND (real current advisory) \u2014 {adv['heading']}"
            print(f"Sending a TEST message using the current live advisory: {adv['heading']!r}")
        else:
            adv = build_test_advisory()
            print("No advisory currently live on PAGASA's page; sending a synthetic TEST message instead.")

        send_discord(webhook_url, adv)
        print("Sent. state.json was not modified.")
        return 0

    html = fetch_page(SOURCE_URL)
    advisories = extract_advisories(html)
    print(f"Fetched page ({len(html)} chars). Extracted {len(advisories)} advisory(ies) currently listed.")
    for adv in advisories:
        print(f"  - [{adv['type']}] {adv['heading']!r} | {adv['issued_at']!r}")

    state = load_state()
    seen_keys = set(state.get("seen", []))

    if not state.get("initialized", False):
        # First-ever run: don't blast out every currently-active advisory,
        # just record what's already there and start watching from here.
        for adv in advisories:
            seen_keys.add(adv["key"])
        state["seen"] = list(seen_keys)
        state["initialized"] = True
        save_state(state)
        print(f"Initialized. Recorded {len(advisories)} existing advisory(ies) as already-seen.")
        return 0

    new_ones = [adv for adv in advisories if adv["key"] not in seen_keys]

    # Post oldest-first so Discord shows them in chronological order
    for adv in reversed(new_ones):
        print(f"New advisory: {adv['heading']} ({adv['issued_at']})")
        send_discord(webhook_url, adv)
        seen_keys.add(adv["key"])

    state["seen"] = list(seen_keys)
    save_state(state)

    if not new_ones:
        print("No new advisories.")
    else:
        print(f"Posted {len(new_ones)} new advisory(ies) to Discord.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
