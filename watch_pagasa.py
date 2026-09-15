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
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

print("hi")

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

DISCORD_COLORS = {
    "thunderstorm watch": 0xF1C40F,       # yellow
    "thunderstorm advisory": 0xE67E22,    # orange
    "thunderstorm information": 0x95A5A6, # grey
    "rainfall advisory": 0x3498DB,        # blue
    "heavy rainfall warning": 0xE74C3C,   # red
}


def fetch_page(url: str) -> str:
    resp = requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; pagasa-ncr-advisory-bot/1.0; "
                "personal weather-alert project)"
            )
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def classify(heading_line: str) -> str:
    """Return which KEYWORDS entry matched a heading line (for coloring/labeling)."""
    ll = heading_line.lower()
    for kw in KEYWORDS:
        if kw in ll:
            return kw
    return "advisory"


def is_heading(line: str) -> bool:
    ll = line.lower()
    has_keyword = any(kw in ll for kw in KEYWORDS)
    has_prsd_tag = "prsd" in ll
    return has_keyword and has_prsd_tag


def is_stop_marker(line: str) -> bool:
    up = line.upper()
    return any(marker in up for marker in STOP_MARKERS)


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

            # The very next non-empty line is expected to be "Issued at: ..."
            j = i + 1
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


def send_discord(webhook_url: str, advisory: dict) -> None:
    color = DISCORD_COLORS.get(advisory["type"], 0x2ECC71)
    body = advisory["body"]
    # Discord embed description limit is 4096 chars; leave headroom
    if len(body) > 3500:
        body = body[:3500] + "\n… (truncated — see full bulletin on PAGASA's site)"

    payload = {
        "embeds": [
            {
                "title": advisory["heading"],
                "description": f"**{advisory['issued_at']}**\n\n{body}",
                "color": color,
                "url": SOURCE_URL,
                "footer": {"text": "Source: DOST-PAGASA NCR-PRSD regional forecast page"},
            }
        ]
    }
    resp = requests.post(webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        return 1

    html = fetch_page(SOURCE_URL)
    advisories = extract_advisories(html)

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
