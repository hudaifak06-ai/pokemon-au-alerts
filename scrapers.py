"""Per-store scrapers.

Each scraper is a function that takes its config dict and returns a list of
Product dicts:
    {
        "store":     "<store key>",
        "sku":       "<stable id, prefer the store's own product id>",
        "title":     "<product title>",
        "url":       "<absolute product url>",
        "price":     "<formatted price string, e.g. '$59.00'>",
        "in_stock":  True | False,
    }

The scrapers try a JSON or structured-data path first (fast, stable) and
fall back to HTML parsing. Where a site uses heavy client-side rendering
(Target AU is the worst offender) the scraper uses Playwright via
`render_html()`.

Scrapers are intentionally defensive: if a store changes its markup, the
scraper logs a warning and returns []. Failures in one store never break
the others.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("pokemon_alerts.scrapers")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _get(url: str, **kw) -> requests.Response | None:
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=20, **kw)
        if r.status_code >= 400:
            log.warning("GET %s -> %s", url, r.status_code)
            return None
        return r
    except requests.RequestException as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def render_html(url: str, wait_selector: str | None = None) -> str | None:
    """Render a page in a headless browser. Used for JS-heavy sites."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed; skipping %s", url)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, locale="en-AU")
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15_000)
                except Exception:
                    pass
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.warning("playwright render failed for %s: %s", url, e)
        return None


def _abs(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(base, href)


def _domain(url: str) -> str:
    return f"{urlparse(url).scheme}://{urlparse(url).netloc}"


# --------------------------------------------------------------------------
# Shopify-based stores (Toymate, many small AU retailers)
# Shopify exposes /products.json which lists products + variants + stock.
# --------------------------------------------------------------------------

def shopify_scrape(store_key: str, search_url: str, query: str = "pokemon") -> list[dict]:
    base = _domain(search_url)
    products: list[dict] = []
    for page in range(1, 6):  # up to 5 pages * 250 = 1250 products
        url = f"{base}/products.json?limit=250&page={page}"
        r = _get(url)
        if not r:
            break
        try:
            data = r.json()
        except ValueError:
            break
        items = data.get("products") or []
        if not items:
            break
        for p in items:
            title = p.get("title", "")
            if query.lower() not in title.lower():
                continue
            handle = p.get("handle")
            variants = p.get("variants") or []
            in_stock = any(v.get("available") for v in variants)
            price = variants[0].get("price") if variants else None
            products.append({
                "store": store_key,
                "sku": f"{store_key}:{p.get('id')}",
                "title": title,
                "url": f"{base}/products/{handle}",
                "price": f"${price}" if price else None,
                "in_stock": in_stock,
            })
        if len(items) < 250:
            break
    return products


# --------------------------------------------------------------------------
# JSON-LD product extractor (works on many AU retailers)
# --------------------------------------------------------------------------

def extract_jsonld_products(html: str, store_key: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            if t == "ItemList":
                for el in node.get("itemListElement", []) or []:
                    item = el.get("item") if isinstance(el, dict) else None
                    if item:
                        nodes.append(item)
                continue
            if t != "Product":
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            avail = (offers.get("availability") or "").lower()
            in_stock = "instock" in avail or "available" in avail
            price = offers.get("price")
            url = node.get("url") or offers.get("url")
            url = _abs(base_url, url) if url else base_url
            sku = node.get("sku") or node.get("productID") or url
            out.append({
                "store": store_key,
                "sku": f"{store_key}:{sku}",
                "title": node.get("name", "")[:300],
                "url": url,
                "price": f"${price}" if price else None,
                "in_stock": in_stock,
            })
    return out


# --------------------------------------------------------------------------
# Store-specific scrapers
# --------------------------------------------------------------------------

def target_au(cfg: dict) -> list[dict]:
    # Target AU uses a heavy SPA; products are loaded via an internal API.
    # The simplest reliable approach is to render the search page.
    html = render_html(cfg["url"], wait_selector="a[href*='/p/']")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    base = _domain(cfg["url"])
    seen, out = set(), []
    for a in soup.select("a[href*='/p/']"):
        href = a.get("href") or ""
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True)[:300]
        if not title:
            continue
        # price + stock live on the product card next to the link
        card = a.find_parent(attrs={"data-locator": True}) or a.parent
        price_el = card.find(string=re.compile(r"\$\s?\d+(?:\.\d{2})?")) if card else None
        out_of_stock = bool(card and "out of stock" in card.get_text(" ", strip=True).lower())
        out.append({
            "store": "target_au",
            "sku": "target_au:" + href.split("/p/")[-1].split("/")[0],
            "title": title,
            "url": _abs(base, href),
            "price": price_el.strip() if price_el else None,
            "in_stock": not out_of_stock,
        })
    return out


def kmart_au(cfg: dict) -> list[dict]:
    r = _get(cfg["url"])
    if not r:
        return []
    items = extract_jsonld_products(r.text, "kmart_au", cfg["url"])
    if items:
        return items
    # Fallback: parse product tiles
    soup = BeautifulSoup(r.text, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select("[data-testid='product-tile'], .product-tile, article"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title = (tile.get("aria-label") or a.get_text(" ", strip=True))[:300]
        price = tile.find(string=re.compile(r"\$\s?\d"))
        oos = "out of stock" in tile.get_text(" ", strip=True).lower()
        out.append({
            "store": "kmart_au",
            "sku": "kmart_au:" + a["href"],
            "title": title,
            "url": _abs(base, a["href"]),
            "price": price.strip() if price else None,
            "in_stock": not oos,
        })
    return out


def bigw(cfg: dict) -> list[dict]:
    r = _get(cfg["url"])
    if not r:
        return []
    return extract_jsonld_products(r.text, "bigw", cfg["url"])


def ebgames(cfg: dict) -> list[dict]:
    # EB Games AU renders the search server-side.
    r = _get(cfg["url"])
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select(".product-container, .product, li.product-tile"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title = (a.get("title") or a.get_text(" ", strip=True))[:300]
        price_el = tile.find(class_=re.compile("price", re.I))
        avail_el = tile.find(class_=re.compile("availability|stock", re.I))
        in_stock = True
        if avail_el and "out" in avail_el.get_text(" ", strip=True).lower():
            in_stock = False
        out.append({
            "store": "ebgames",
            "sku": "ebgames:" + a["href"],
            "title": title,
            "url": _abs(base, a["href"]),
            "price": price_el.get_text(" ", strip=True) if price_el else None,
            "in_stock": in_stock,
        })
    return out


def jbhifi(cfg: dict) -> list[dict]:
    r = _get(cfg["url"])
    if not r:
        return []
    items = extract_jsonld_products(r.text, "jbhifi", cfg["url"])
    if items:
        return items
    # Fallback to embedded __NEXT_DATA__ JSON
    m = re.search(r'__NEXT_DATA__"[^>]*>(\{.*?\})</script>', r.text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out = []
    # Walk the tree looking for product objects
    def walk(node):
        if isinstance(node, dict):
            if {"name", "sku"} <= node.keys() and "url" in node:
                out.append({
                    "store": "jbhifi",
                    "sku": "jbhifi:" + str(node["sku"]),
                    "title": node["name"][:300],
                    "url": _abs("https://www.jbhifi.com.au", node["url"]),
                    "price": f"${node.get('priceInfo', {}).get('current') or node.get('price')}",
                    "in_stock": bool(node.get("inStockOnline") or node.get("isInStock")),
                })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    return out


def zing(cfg: dict) -> list[dict]:
    # Zing runs on Shopify
    return shopify_scrape("zing", cfg["url"])


def toymate(cfg: dict) -> list[dict]:
    # Toymate runs on Shopify
    return shopify_scrape("toymate", cfg["url"])


def costco_au(cfg: dict) -> list[dict]:
    # Costco AU requires a member login to view stock. Skipped by default.
    log.info("Costco AU requires login; skipping.")
    return []


# ---- New stores with in-store pickup --------------------------------------

def officeworks(cfg: dict) -> list[dict]:
    """Officeworks: huge Click & Collect network. Pages embed JSON-LD."""
    r = _get(cfg["url"])
    if not r:
        return []
    items = extract_jsonld_products(r.text, "officeworks", cfg["url"])
    if items:
        return items
    # Fallback: parse product cards
    soup = BeautifulSoup(r.text, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select("[data-ref='product-card'], article.product-card, .ow-product-card"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title = (a.get("title") or a.get_text(" ", strip=True))[:300]
        price_el = tile.find(class_=re.compile("price", re.I))
        oos = "out of stock" in tile.get_text(" ", strip=True).lower()
        out.append({
            "store": "officeworks",
            "sku": "officeworks:" + a["href"],
            "title": title,
            "url": _abs(base, a["href"]),
            "price": price_el.get_text(" ", strip=True) if price_el else None,
            "in_stock": not oos,
        })
    return out


def woolworths(cfg: dict) -> list[dict]:
    """Woolworths: uses an internal JSON Search API. Needs session cookies."""
    sess = requests.Session()
    sess.headers.update(DEFAULT_HEADERS)
    try:
        # Prime the session so the API accepts our request
        sess.get(cfg["url"], timeout=20)
        r = sess.post(
            "https://www.woolworths.com.au/apis/ui/Search/products",
            json={
                "SearchTerm": "pokemon",
                "PageSize": 60,
                "PageNumber": 1,
                "SortType": "TraderRelevance",
                "Location": cfg["url"],
                "Filters": [],
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        if not r.ok:
            log.warning("Woolworths API %s", r.status_code)
            return []
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Woolworths fetch failed: %s", e)
        return []
    out = []
    for product in data.get("Products", []) or []:
        for item in product.get("Products", []) or [product]:
            stockcode = item.get("Stockcode")
            name = item.get("Name") or item.get("DisplayName") or ""
            if not stockcode or not name:
                continue
            out.append({
                "store": "woolworths",
                "sku": f"woolworths:{stockcode}",
                "title": name[:300],
                "url": f"https://www.woolworths.com.au/shop/productdetails/{stockcode}",
                "price": f"${item.get('Price')}" if item.get("Price") else None,
                "in_stock": bool(item.get("IsAvailable", True)) and not item.get("IsOutOfStock"),
            })
    return out


def coles(cfg: dict) -> list[dict]:
    """Coles: aggressive Akamai protection. Uses Playwright."""
    html = render_html(cfg["url"], wait_selector="[data-testid='product-tile'], h2")
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select("[data-testid='product-tile'], .product-tile, section.product"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title = (a.get("aria-label") or a.get_text(" ", strip=True))[:300]
        price_el = tile.find(class_=re.compile("price", re.I))
        oos = "unavailable" in tile.get_text(" ", strip=True).lower() \
              or "out of stock" in tile.get_text(" ", strip=True).lower()
        out.append({
            "store": "coles",
            "sku": "coles:" + a["href"],
            "title": title,
            "url": _abs(base, a["href"]),
            "price": price_el.get_text(" ", strip=True) if price_el else None,
            "in_stock": not oos,
        })
    return out


def smyths_toys(cfg: dict) -> list[dict]:
    """Smyths Toys AU: SAP Commerce, server-rendered listings."""
    r = _get(cfg["url"])
    if not r:
        return []
    items = extract_jsonld_products(r.text, "smyths_toys", cfg["url"])
    if items:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select(".product-tile, .product-card, li.product"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title_el = tile.find(class_=re.compile("title|name", re.I)) or a
        price_el = tile.find(class_=re.compile("price", re.I))
        oos = "out of stock" in tile.get_text(" ", strip=True).lower()
        out.append({
            "store": "smyths_toys",
            "sku": "smyths_toys:" + a["href"],
            "title": title_el.get_text(" ", strip=True)[:300],
            "url": _abs(base, a["href"]),
            "price": price_el.get_text(" ", strip=True) if price_el else None,
            "in_stock": not oos,
        })
    return out


def toyworld(cfg: dict) -> list[dict]:
    """Toyworld AU: BigCommerce-based central catalogue."""
    r = _get(cfg["url"])
    if not r:
        return []
    items = extract_jsonld_products(r.text, "toyworld", cfg["url"])
    if items:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select("li.product, article.card, .product-item"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title_el = tile.find(class_=re.compile("title|name", re.I)) or a
        price_el = tile.find(class_=re.compile("price", re.I))
        oos = "out of stock" in tile.get_text(" ", strip=True).lower() \
              or "sold out" in tile.get_text(" ", strip=True).lower()
        out.append({
            "store": "toyworld",
            "sku": "toyworld:" + a["href"],
            "title": title_el.get_text(" ", strip=True)[:300],
            "url": _abs(base, a["href"]),
            "price": price_el.get_text(" ", strip=True) if price_el else None,
            "in_stock": not oos,
        })
    return out


def good_games(cfg: dict) -> list[dict]:
    """Good Games: Shopify."""
    return shopify_scrape("good_games", cfg["url"])


def gamesmen(cfg: dict) -> list[dict]:
    """The Gamesmen (Sydney): BigCommerce, server-rendered."""
    r = _get(cfg["url"])
    if not r:
        return []
    items = extract_jsonld_products(r.text, "gamesmen", cfg["url"])
    if items:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select("li.product, article.card"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title_el = tile.find(class_=re.compile("title|name", re.I)) or a
        price_el = tile.find(class_=re.compile("price", re.I))
        oos = "out of stock" in tile.get_text(" ", strip=True).lower()
        out.append({
            "store": "gamesmen",
            "sku": "gamesmen:" + a["href"],
            "title": title_el.get_text(" ", strip=True)[:300],
            "url": _abs(base, a["href"]),
            "price": price_el.get_text(" ", strip=True) if price_el else None,
            "in_stock": not oos,
        })
    return out


def card_crusade(cfg: dict) -> list[dict]:
    """Card Crusade: Shopify."""
    return shopify_scrape("card_crusade", cfg["url"])


def sanity(cfg: dict) -> list[dict]:
    """Sanity: HTML tiles, server-rendered."""
    r = _get(cfg["url"])
    if not r:
        return []
    items = extract_jsonld_products(r.text, "sanity", cfg["url"])
    if items:
        return items
    soup = BeautifulSoup(r.text, "lxml")
    base = _domain(cfg["url"])
    out = []
    for tile in soup.select(".product, .product-tile, li.product-item"):
        a = tile.find("a", href=True)
        if not a:
            continue
        title_el = tile.find(class_=re.compile("title|name", re.I)) or a
        price_el = tile.find(class_=re.compile("price", re.I))
        oos = "unavailable" in tile.get_text(" ", strip=True).lower() \
              or "out of stock" in tile.get_text(" ", strip=True).lower()
        out.append({
            "store": "sanity",
            "sku": "sanity:" + a["href"],
            "title": title_el.get_text(" ", strip=True)[:300],
            "url": _abs(base, a["href"]),
            "price": price_el.get_text(" ", strip=True) if price_el else None,
            "in_stock": not oos,
        })
    return out


SCRAPERS: dict[str, Callable[[dict], list[dict]]] = {
    # Original AU retailers with Click & Collect
    "target_au":    target_au,
    "kmart_au":     kmart_au,
    "bigw":         bigw,
    "ebgames":      ebgames,
    "jbhifi":       jbhifi,
    "zing":         zing,
    "toymate":      toymate,
    "costco_au":    costco_au,
    # Added: more chains with Click & Collect / in-store pickup
    "officeworks":  officeworks,
    "woolworths":   woolworths,
    "coles":        coles,
    "smyths_toys":  smyths_toys,
    "toyworld":     toyworld,
    # Added: hobby/specialty with in-store pickup
    "good_games":   good_games,
    "gamesmen":     gamesmen,
    "card_crusade": card_crusade,
    "sanity":       sanity,
}
