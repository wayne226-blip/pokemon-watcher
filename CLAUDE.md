# CLAUDE.md — Pokémon stock watcher

Read this before touching anything. The README is the user guide; this file is
how to work on it safely. **This repo is PUBLIC** — never commit a token, chat
id, ping URL or API key. Secrets live only in GitHub secrets.

## What it is

Wayne (UK collector, buys for himself — never resale) wants to know the moment
Pokémon TCG sealed product is buyable **at a sane price**. GitHub Actions runs
`python -m pokemon_watcher.run --once` every ~5 min, reads 4 UK shops, and
sends Pokémon-themed Telegram alerts. It detects and alerts only; it never buys.

## Shops and how each is read (all verified 24 Oct 2026)

| Shop key | Site | Reader | Gotchas |
|---|---|---|---|
| `totalcards` | totalcards.net | Shopify `search/suggest.json` (+ whole collection `products.json`) | 429 after ~10 quick hits. `£0.00` = unpriced. After selling out keeps its price with `available:false`. `.js` prices are pence |
| `zatu` | zatu.com | same Shopify reader | Marks up old/sold-out stock (30th ETB £169.99); new releases at RRP |
| `tgc` | collect.thegamecollection.net | Shopify search **only** | Its `.js` and `products.json` feeds 429 on the first request; search works |
| `magicmadhouse` | magicmadhouse.co.uk | product page via **curl**, embedded BigCommerce JSON (`"instock"`, `"stock"`) | Cloudflare blocks Python httpx from GitHub runners, curl passes. "Add to Cart" shows even at stock 0 — not a signal. Category pages load via Klevu — **don't call Klevu directly** (treated as circumvention) |

Not readable / don't try: Pokémon Center UK, Smyths (Imperva CAPTCHA), Argos,
Chaos Cards, Firestorm, Big Orbit, GAME, John Lewis, HMV, Very, Packratt (JS).
Wayne covers those with **TCG Stock** (Discord) — don't build around blocks.

## Layout

- `watchlist.yaml` — products, whole collections, price caps, TCG sets, summary hours. **Edit here, not in code.**
- `totalcards.py` — Shopify search reader for all Shopify shops (`SHOPIFY_BASES`); searches interleave shops to avoid rate limits
- `collections.py` — whole-collection reader, sealed-only filter (skips singles, empty tins, stickers, art cards, coins, non-English)
- `magicmadhouse.py` — curl fetch + BigCommerce JSON parse
- `state.py` — `decide()`: every alert rule, pure and tested. Per-kind cooldowns; silent first-run baseline; price tolerance (25% over `shop_price`) or hard `max_price` caps; sold-out-again; NOT_SEEN never alerts
- `tcgapi.py` — Pokémon TCG API set info + chase cards (by rarity — no prices for 2026 sets). API is flaky (500/502), so it's **cached in `state.json` in the background**; BUY NOW reads the cache, never the network
- `report.py` — "Pokédex update" summary (Telegram HTML, tap-to-expand quotes per shop)
- `notify.py` — Telegram only (ntfy optional/off). Photo alerts via sendPhoto, falls back to text; Markdown/HTML rejected → resent plain
- `run.py` — one pass; also `--test-notify`, `--test-buy-now`, `--report-now`; pings healthchecks.io at the end

## Timing and health

- GitHub's own cron is best-effort and barely fires. **cron-job.org** (Wayne's account) triggers the workflow every 5 min via the API with a fine-grained token (Actions read/write on this repo only) — **expires 24 Oct 2027**.
- **healthchecks.io** dead man's switch (check "Pokémon watcher", 5-min period, 25-min grace) messages Telegram if pings stop.
- **Fast lane** (`fast_lane` in `watchlist.yaml`): after the main pass, must-have products get extra checks about a minute apart; the last round must finish by ~245 s (next trigger can come ~280 s after the last), so drops are seen in ~1–2 min. Stock news only (errors stay on the 5-min pass). Turns itself off after `until`.
- Summaries at 9, 13, 21 UK time. If they stop arriving, something's broken.
- The workflow commits `state.json` every run (keeps the repo active past GitHub's 60-day rule); the push retries 3×.

## Rules when changing it

- **TDD.** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest`. Fixtures in `tests/fixtures/` are real captured responses.
- Be polite to shops: few requests per run, gaps between them. Capture new fixtures slowly (≥5 s apart).
- A BUY NOW must never be delayed or lost: nothing slow or flaky may run before alerts are sent.
- Adding a Shopify product: confirm its search `query` returns the handle in the top 10.
- Copy is Pokémon-themed and fun on purpose (Wayne asked) — BUY NOW the most fun.
