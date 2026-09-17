#!/usr/bin/env python3
"""
watch_qc_suspension.py

Watches the Quezon City government's official website (quezoncity.gov.ph)
for new "Walang Pasok" (class/work suspension) announcements, and posts
each one -- including the actual official graphic QC Gov publishes -- to
a Discord webhook.

Why this source: quezoncity.gov.ph is a standard WordPress site (not a
JS-rendered map like panahon.gov.ph), so it exposes a normal RSS feed at
/feed/, and every post embeds its official announcement image in a plain
<meta property="og:image"> tag. That means we can fetch the *real* graphic
QC Gov posts -- not a recreation -- directly from their own site, with no
Facebook scraping involved.

Curated announcements (including every "Walang Pasok" post) also show up at
https://quezoncity.gov.ph/announcements/ if you want to sanity-check this
against what the feed is returning.

State (which announcements we've already alerted on) is kept in
qc_state.json so this is safe to run on a schedule without duplicate alerts.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

FEED_URL = "https://quezoncity.gov.ph/feed/"
STATE_PATH = Path(__file__).parent / "qc_state.json"
MAX_SEEN_KEYS = 300

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; qc-suspension-watcher/1.0; "
        "personal weather-alert project)"
    )
}

# A title is treated as a suspension announcement if it contains any of these
# (case-insensitive). Loose on purpose: QC Gov's titles vary a lot
# ("Walang Pasok - ...", "#WALANG PASOK - Class and Work Suspension - ...",
# "Public Service Announcement: City-Wide Class Suspension - ...").
SUSPENSION_KEYWORDS = [
    "walang pasok",
    "class suspension",
    "suspension of class",
    "suspension of classes",
    "work suspension",
    "class and work suspension",
]


def is_suspension_post(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in SUSPENSION_KEYWORDS)


def fetch_feed_items() -> list[dict]:
    resp = requests.get(FEED_URL, headers=UA_HEADERS, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description = (item.findtext("description") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if title and link:
            items.append(
                {
                    "title": title,
                    "link": link,
                    "pub_date": pub_date,
                    "description": description,
                    "guid": guid,
                }
            )
    return items


def clean_html_text(html_snippet: str) -> str:
    return BeautifulSoup(html_snippet, "html.parser").get_text(" ", strip=True)


def fetch_og_image(post_url: str) -> str | None:
    resp = requests.get(post_url, headers=UA_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find("meta", attrs={"property": "og:image"})
    if tag and tag.get("content"):
        return tag["content"]
    return None


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


def send_discord(webhook_url: str, item: dict, image_url: str | None) -> None:
    description = clean_html_text(item["description"])
    if len(description) > 3500:
        description = description[:3500] + "\n… (truncated — see full post on QC Gov's site)"

    embed = {
        "title": item["title"],
        "description": description,
        "url": item["link"],
        "color": 0xC0392B,  # red — suspension announcements are urgent
        "footer": {"text": "Source: Quezon City Government (quezoncity.gov.ph)"},
    }
    if image_url:
        embed["image"] = {"url": image_url}

    payload = {
        "content": "\U0001F514 **QC Class/Work Suspension Announcement**",
        "embeds": [embed],
    }
    resp = requests.post(webhook_url, json=payload, timeout=20)
    resp.raise_for_status()


def build_test_item() -> dict:
    """A guaranteed, clearly-labeled sample announcement for --test mode."""
    return {
        "title": "\U0001F9EA TEST MESSAGE \u2014 Walang Pasok (this is not a real announcement)",
        "link": "https://quezoncity.gov.ph/announcements/",
        "description": (
            "<p>This is a TEST message from your QC suspension watcher, sent to confirm the "
            "Discord webhook is working end-to-end. This is not an actual suspension "
            "announcement.</p>"
        ),
        "pub_date": "",
        "guid": "test-message-not-real",
    }


def main() -> int:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.", file=sys.stderr)
        return 1

    if "--test" in sys.argv:
        # Prefer using the most recent real post's actual image (best test of
        # the full pipeline); fall back to a synthetic sample with no image
        # if that fails for any reason. Either way, qc_state.json is untouched.
        image_url = None
        try:
            items = fetch_feed_items()
        except requests.RequestException as exc:
            print(f"Could not fetch the live feed ({exc}); using a synthetic sample instead.")
            items = []

        if items:
            item = dict(items[0])
            item["title"] = f"\U0001F9EA TEST SEND (using real latest post) \u2014 {item['title']}"
            try:
                image_url = fetch_og_image(items[0]["link"])
            except requests.RequestException:
                image_url = None
            print(f"Sending a TEST message using the most recent real post: {items[0]['title']!r}")
        else:
            item = build_test_item()
            print("Could not reach the live feed; sending a fully synthetic TEST message instead.")

        send_discord(webhook_url, item, image_url)
        print("Sent. qc_state.json was not modified.")
        return 0

    items = fetch_feed_items()
    suspension_items = [i for i in items if is_suspension_post(i["title"])]

    state = load_state()
    seen_keys = set(state.get("seen", []))

    if not state.get("initialized", False):
        for item in suspension_items:
            seen_keys.add(item["guid"])
        state["seen"] = list(seen_keys)
        state["initialized"] = True
        save_state(state)
        print(f"Initialized. Recorded {len(suspension_items)} existing suspension post(s) as already-seen.")
        return 0

    new_items = [i for i in suspension_items if i["guid"] not in seen_keys]

    for item in reversed(new_items):  # oldest first, chronological order in Discord
        print(f"New suspension announcement: {item['title']}")
        try:
            image_url = fetch_og_image(item["link"])
        except requests.RequestException:
            image_url = None
        send_discord(webhook_url, item, image_url)
        seen_keys.add(item["guid"])

    state["seen"] = list(seen_keys)
    save_state(state)

    if not new_items:
        print("No new suspension announcements.")
    else:
        print(f"Posted {len(new_items)} new suspension announcement(s) to Discord.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
