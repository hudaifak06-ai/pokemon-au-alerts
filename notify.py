"""Discord webhook notifier.

Reads webhook URL from the DISCORD_WEBHOOK_URL environment variable.
Posts a rich embed for each new restock event.
"""
from __future__ import annotations

import os
import time
import requests
from typing import Iterable

STORE_COLORS = {
    "target_au":    0xE52E2E,
    "kmart_au":     0xE2231A,
    "bigw":         0x004B9F,
    "officeworks":  0x004F9F,
    "woolworths":   0x178841,
    "coles":        0xE01A22,
    "ebgames":      0xC8102E,
    "jbhifi":       0xFFEB00,
    "zing":         0xFF6F00,
    "smyths_toys":  0xE3001B,
    "toyworld":     0x009FE3,
    "toymate":      0x00ADEF,
    "sanity":       0xE6007E,
    "good_games":   0x111111,
    "gamesmen":     0x1E3A8A,
    "card_crusade": 0x8E2B85,
    "costco_au":    0xE32B2B,
}

STORE_LABELS = {
    "target_au":    "Target AU",
    "kmart_au":     "Kmart AU",
    "bigw":         "Big W",
    "officeworks":  "Officeworks",
    "woolworths":   "Woolworths",
    "coles":        "Coles",
    "ebgames":      "EB Games",
    "jbhifi":       "JB Hi-Fi",
    "zing":         "Zing Pop Culture",
    "smyths_toys":  "Smyths Toys AU",
    "toyworld":     "Toyworld",
    "toymate":      "Toymate",
    "sanity":       "Sanity",
    "good_games":   "Good Games",
    "gamesmen":     "The Gamesmen",
    "card_crusade": "Card Crusade",
    "costco_au":    "Costco AU",
}


def post_events(events: Iterable[dict]) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("DISCORD_WEBHOOK_URL not set; printing events instead.")
        for ev in events:
            print(ev)
        return

    events = list(events)
    if not events:
        print("No new restocks.")
        return

    # Discord webhooks accept up to 10 embeds per message.
    for i in range(0, len(events), 10):
        chunk = events[i:i + 10]
        embeds = [_build_embed(ev) for ev in chunk]
        resp = requests.post(
            webhook,
            json={"content": "**Pokemon restock alert!**", "embeds": embeds},
            timeout=15,
        )
        if resp.status_code == 429:
            retry = float(resp.json().get("retry_after", 2))
            time.sleep(retry + 0.5)
            requests.post(webhook, json={"embeds": embeds}, timeout=15)
        elif not resp.ok:
            print(f"Webhook error {resp.status_code}: {resp.text[:200]}")


def _build_embed(event: dict) -> dict:
    store = event.get("store", "")
    return {
        "title": event["title"][:250],
        "url": event["url"],
        "color": STORE_COLORS.get(store, 0x808080),
        "fields": [
            {"name": "Store", "value": STORE_LABELS.get(store, store), "inline": True},
            {"name": "Price", "value": event.get("price") or "—", "inline": True},
            {"name": "Status", "value": event.get("status", "In stock"), "inline": True},
        ],
        "footer": {"text": "Pokemon restock monitor"},
        "timestamp": event.get("timestamp"),
    }
