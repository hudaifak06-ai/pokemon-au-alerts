"""Pokemon restock alert runner.

For each enabled store, scrape the search/category page, compare against
the previous snapshot, and post Discord alerts for products that:
  * flipped from out-of-stock -> in-stock, OR
  * are brand new and in stock.

State is persisted to state.json so re-runs only alert on real changes.

Usage:
    python check.py [--config config.yaml] [--state state.json] [--once]

In CI (GitHub Actions) this runs on a cron schedule and commits the
updated state.json back to the repo so it persists between runs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import sys
import time
import traceback

import yaml

from scrapers import SCRAPERS
from notify import post_events

log = logging.getLogger("pokemon_alerts")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("state file corrupt; starting fresh")
        return {}


def save_state(path: str, state: dict) -> None:
    pathlib.Path(path).write_text(json.dumps(state, indent=2), encoding="utf-8")


def matches_filters(product: dict, cfg: dict) -> bool:
    title = product["title"].lower()
    keywords = [str(k).lower() for k in cfg.get("keywords", []) or []]
    if keywords and not any(k in title for k in keywords):
        return False
    watch = [str(w).lower() for w in cfg.get("watchlist") or []]
    if watch and not any(w in title for w in watch):
        return False
    return True


def diff_and_alert(products: list[dict], state: dict, cfg: dict) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    cooldown = dt.timedelta(minutes=cfg.get("alerts", {}).get("cooldown_minutes", 60))
    on_price = cfg.get("alerts", {}).get("on_price_change", False)
    events: list[dict] = []

    for p in products:
        if not matches_filters(p, cfg):
            continue
        sku = p["sku"]
        prev = state.get(sku, {})
        was_in_stock = prev.get("in_stock", False)
        last_alert = prev.get("last_alert")
        if last_alert:
            try:
                last_alert_dt = dt.datetime.fromisoformat(last_alert)
            except ValueError:
                last_alert_dt = None
        else:
            last_alert_dt = None

        should_alert = False
        reason = ""
        if p["in_stock"] and not was_in_stock:
            should_alert = True
            reason = "Restock" if prev else "New listing"
        elif on_price and p["in_stock"] and prev.get("price") and p.get("price") and p["price"] != prev["price"]:
            should_alert = True
            reason = f"Price change ({prev['price']} → {p['price']})"

        if should_alert and last_alert_dt and (now - last_alert_dt) < cooldown:
            should_alert = False  # cooldown still active

        if should_alert:
            events.append({
                **p,
                "status": reason,
                "timestamp": now.isoformat(),
            })
            state[sku] = {
                "title": p["title"],
                "in_stock": p["in_stock"],
                "price": p.get("price"),
                "last_alert": now.isoformat(),
                "last_seen": now.isoformat(),
            }
        else:
            state[sku] = {
                "title": p["title"],
                "in_stock": p["in_stock"],
                "price": p.get("price"),
                "last_alert": prev.get("last_alert"),
                "last_seen": now.isoformat(),
            }
    return events


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default="state.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    state = load_state(args.state)

    all_products: list[dict] = []
    for store_key, store_cfg in cfg.get("stores", {}).items():
        if not store_cfg.get("enabled"):
            continue
        scraper = SCRAPERS.get(store_key)
        if not scraper:
            log.warning("no scraper for store %s", store_key)
            continue
        log.info("Scraping %s ...", store_key)
        t0 = time.time()
        try:
            products = scraper(store_cfg) or []
        except Exception:
            log.error("scraper %s crashed:\n%s", store_key, traceback.format_exc())
            products = []
        log.info("  -> %d products in %.1fs", len(products), time.time() - t0)
        all_products.extend(products)

    events = diff_and_alert(all_products, state, cfg)
    log.info("%d new restock events", len(events))
    if events:
        post_events(events)
    save_state(args.state, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
