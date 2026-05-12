# Pokemon AU Restock Alerts

A free, self-hosted restock alert bot for major Australian Pokemon TCG
retailers. Scrapes each store's Pokemon listings on a schedule, diffs
against the previous run, and posts Discord embeds when something flips
from out-of-stock to in-stock (or a new product appears).

## Stores covered

Every store below offers Click & Collect or in-store pickup in Australia,
which is what this bot is tuned for.

**Big-box & department**

| Store | Method | Notes |
| --- | --- | --- |
| Target AU | Playwright | Slow but reliable; aggressive bot detection |
| Kmart AU | JSON-LD | Fast |
| Big W | JSON-LD | Fast |
| Officeworks | JSON-LD | Click & Collect at ~170 stores |
| Woolworths | Internal Search API | Direct to Boot pickup; needs session cookies |
| Coles | Playwright | Akamai-protected; Click & Collect nationwide |

**Toy & entertainment chains**

| Store | Method | Notes |
| --- | --- | --- |
| EB Games AU | HTML tiles | Server-rendered |
| JB Hi-Fi | JSON-LD + `__NEXT_DATA__` | Fast |
| Zing Pop Culture | Shopify `/products.json` | Very reliable |
| Smyths Toys AU | JSON-LD | Reserve & Collect |
| Toyworld | JSON-LD | ~80 franchise stores; pickup at participating stores |
| Toymate | Shopify `/products.json` | Very reliable |
| Sanity | JSON-LD | Click & Collect |

**Hobby / specialty**

| Store | Method | Notes |
| --- | --- | --- |
| Good Games | Shopify `/products.json` | In-store pickup, gets distributor allocations |
| The Gamesmen | JSON-LD / BigCommerce | Sydney pickup |
| Card Crusade | Shopify `/products.json` | Multi-location pickup |

**Disabled**

| Store | Why |
| --- | --- |
| Costco AU | Membership login required to view stock |
| Mighty Ape AU | NZ warehouse, no AU physical pickup |

> Walmart and Best Buy don't operate in Australia, so they're not
> included. If you want them for a US setup, the scraper layout in
> `scrapers.py` makes them easy to add — but expect a real fight with
> Akamai/PerimeterX bot protection on both.

## How it works

1. `check.py` reads `config.yaml` to find which stores to hit and which
   keywords/SKUs to watch.
2. For each enabled store, the matching scraper in `scrapers.py` returns
   a list of `{store, sku, title, url, price, in_stock}` dicts.
3. Results are diffed against `state.json` from the previous run.
4. Any product that flipped to in-stock is sent to Discord as an embed
   via webhook.
5. The updated state is written back; in CI it's committed to the repo
   so the next run sees it.

## Set up the Discord webhook

1. In Discord, open the channel you want alerts in -> **Edit Channel** ->
   **Integrations** -> **Webhooks** -> **New Webhook**.
2. Copy the webhook URL.
3. For local testing: `export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."`.
4. For GitHub Actions: repo **Settings** -> **Secrets and variables** ->
   **Actions** -> **New repository secret** named `DISCORD_WEBHOOK_URL`.

## Run it locally

```bash
cd pokemon_alerts
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium    # only needed for Target AU
export DISCORD_WEBHOOK_URL="..."
python check.py
```

First run will report a lot of "new" listings — that's expected, it's
populating `state.json`. After that, only real restocks fire.

## Deploy it free on GitHub Actions

1. Push this folder to a **public** GitHub repo. (Public repos get
   unlimited free Actions minutes; private repos get 2,000/month, which
   is still plenty.)
2. Add the `DISCORD_WEBHOOK_URL` secret as above.
3. The workflow in `.github/workflows/check.yml` runs every 10 minutes
   and commits the updated `state.json` back so subsequent runs know
   what was already in stock.

If you want it faster than 10 minutes, drop the cron to `*/5` — that
gives ~8,640 runs/month at ~1 min each, still fine for a public repo.

## Tuning what gets alerted

Edit `config.yaml`:

- `keywords`: a product must contain at least one of these in its title
  to even be considered. Keeps non-Pokemon junk out of search results.
- `watchlist`: if set, only products matching one of these (e.g.
  `"prismatic evolutions"`) alert. Leave empty to alert on everything
  Pokemon.
- `stores.<name>.enabled`: turn stores on/off.
- `alerts.cooldown_minutes`: minimum gap between alerts for the same
  product, so you don't get spammed if a site flickers.

## Notes on reliability

These scrapers parse HTML and structured data; sites change. The code
is intentionally defensive: a broken scraper logs a warning and returns
`[]`, so one store breaking won't take the rest down. If you start
missing alerts from a specific store, open that store's search URL in
a browser, look at what the product tiles render as now, and update the
relevant function in `scrapers.py`.

**Target AU** is the most fragile because it's a heavy SPA with bot
detection. If Playwright starts getting blocked, options include:
running with a residential proxy (not free), or polling a smaller AU
retailer's listings of the same set instead.

## Beyond this starter kit

- **Per-product subscriptions:** add a `@subscribe <keyword>` Discord
  bot command (requires a real bot, not just a webhook) so users can
  opt in to specific sets.
- **Auto-checkout:** don't. Scalper bots violate every retailer's ToS
  and Discord's; restock alerts for human shoppers are fine.
- **More stores:** Catch.com.au, OzGameShop, Card Crusade, Good Games,
  The Gamesmen — all easy adds. Most run on Shopify or BigCommerce, so
  the existing `shopify_scrape` helper works as-is.
- **Free aggregation sources:** subscribe a webhook to
  `r/PokemonTCGAustralia` RSS via a service like MonitoRSS, and to
  TCGplayer/eBay AU saved-search RSS feeds, to fill the gaps when your
  scrapers miss something.
