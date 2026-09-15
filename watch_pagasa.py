#!/usr/bin/env python3
"""
watch_pagasa.py
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from render_card import render_advisory_card

SOURCE_URL = "https://pagasa.dost.gov.ph/regional-forecast/ncrprsd"
STATE_PATH = Path(__file__).parent / "state.json"

MAX_SEEN_KEYS = 300

KEYWORDS = [
    "rainfall advisory",
    "heavy rainfall warning",
    "thunderstorm advisory",
    "thunderstorm watch",
    "thunderstorm information",
]

STOP_MARKERS = [
    "SPECIAL FORECAST",
    "IMPACT BASED FORECAST",
    "GREATER METRO MANILA AREA FORECAST",
    "RADAR MOSAIC",
    "SATELLITE LAYERS",
    "ACTIVE WARNINGS",
    "LATEST WEATHER",
]

# Keywords to strictly filter for Metro Manila & its cities
METRO_MANILA_KEYWORDS = [
    "metro manila",
    "manila",
    "quezon city",
    "caloocan",
    "las piñas",
    "makati",
    "malabon",
    "mandaluyong",
    "marikina",
    "muntinlupa",
    "navotas",
    "parañaque",
    "pasay",
    "pasig",
    "san juan",
    "taguig",
    "valenzuela",
    "pateros",
]

DISCORD_COLORS = {
    "thunderstorm watch": 0xF1C40F,
    "thunderstorm advisory": 0xE67E22,
    "thunderstorm information": 0x95A5A6,
    "rainfall advisory": 0x3498DB,
    "heavy rainfall warning": 0xE74C3C,
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
    soup = BeautifulSoup(html, "html.parser")
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

            j = i + 1
            if j < n and lines[j].lower().startswith("issued at"):
                issued_at = lines[j]
                j += 1

            while j < n and not is_heading(lines[j]) and not is_stop_marker(lines[j]):
                body_lines.append(lines[j])
                j += 1

            body = "\n".join(body_lines).strip()
            adv_type = classify(heading)
            key_source = f"{heading}|{issued_at}"
            key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()

            # Location Filter: Only include advisories targeting Metro Manila cities
            full_content = f"{heading} {body}".lower()
            if any(place in full_content for place in METRO_MANILA_KEYWORDS):
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
    state["seen"] = state["seen"][-MAX_SEEN_KEYS:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def send_discord(webhook_url: str, advisory: dict) -> None:
    color = DISCORD_COLORS.get(advisory["type"], 0x2ECC71)
    body = advisory["body"]
    if len(body) > 3500:
        body = body[:3500] + "\n… (truncated — see full bulletin on PAGASA's site)"

    adv_key = advisory.get("key", "test123456")
    filename = f"advisory_{adv_key[:8]}.png"

    payload = {
        "content": "🚨 **METRO MANILA may be affected**",
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

    image_bytes = render_advisory_card(advisory)
    resp = requests.post(
        webhook_url,
        data={"payload_json": json.dumps(payload)},
        files={"file": (filename, image_bytes, "image/png")},
        timeout=30,
    )
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
        for adv in advisories:
            seen_keys.add(adv["key"])
        state["seen"] = list(seen_keys)
        state["initialized"] = True
        save_state(state)
        print(f"Initialized. Recorded {len(advisories)} existing Metro Manila advisory(ies) as seen.")
        return 0

    new_ones = [adv for adv in advisories if adv["key"] not in seen_keys]

    for adv in reversed(new_ones):
        print(f"New Metro Manila advisory: {adv['heading']} ({adv['issued_at']})")
        send_discord(webhook_url, adv)
        seen_keys.add(adv["key"])

    state["seen"] = list(seen_keys)
    save_state(state)

    if not new_ones:
        print("No new Metro Manila advisories.")
    else:
        print(f"Posted {len(new_ones)} new advisory(ies) to Discord.")

    return 0

def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
