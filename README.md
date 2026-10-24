# Pokémon Stock Watcher

Checks UK hobby card shops every ~5 minutes for new Pokémon TCG sets and pushes
an alert to Telegram the moment something can be bought or
pre-ordered at a sane price. **It detects and alerts only — it never buys.**

Runs free on GitHub Actions, so it keeps going while the laptop is shut.

## What it watches

Edit [`watchlist.yaml`](watchlist.yaml) — never the code.

| Shop | Products | How it reads them |
|---|---|---|
| Total Cards | 30th Celebration ETB, Delta Reign (ETB, bundle, boxes, pack, 4 blisters), 30th Celebration Ultra-Premium Collection Day + Night | Shopify search requests, one per query |
| Zatu | 30th Celebration ETB, Delta Reign ETB, booster box, bundle | same Shopify reader |
| The Game Collection | Delta Reign (ETB, box, bundle, blister, checklane), 30th ETB, Greninja/Sylveon ex boxes, 9 mini tins — at or under shop price | Shopify search only (its product/collection feeds rate-limit instantly) |
| Magic Madhouse | 30th Celebration ETB, Delta Reign ETB, booster box, pack | the stock data embedded in each product page |
| Total Cards + Zatu | **every sealed 30th Celebration product** (mini tins, ETB, binder, ex boxes and tins, UPC, figure collections…), including ones added later | whole-collection reads, one request per 250 products |

Collection products have no `shop_price`; instead `price_caps` in
`watchlist.yaml` sets a max price by words in the title (e.g. mini tin £22,
ETB £60). Over the cap = no alert, just shown in the summaries.

**Not watched, and why:** Pokémon Center UK (bot challenge), Chaos Cards,
Firestorm, Smyths, Argos (all return 403 to scripts), Amazon (invite-based —
that signal arrives by email). Use Distill.io in your own Chrome for those.

## Alerts

All alerts are Pokémon-themed — a BUY NOW reads "⚡ A wild … appeared!" with a
🛒 button straight to the product page.

BUY NOW alerts also carry the set's info and its top 3 chase cards (by rarity)
from the free [Pokémon TCG API](https://pokemontcg.io), sent as a photo of the
best card. The API is often down, so that info is fetched in the background
(a couple of sets per run, retried until it works) and saved in `state.json`;
a BUY NOW reads the saved copy and never waits on the API. Sets are listed under `tcg_sets`
in `watchlist.yaml`; an optional `POKEMONTCG_API_KEY` secret raises its limits.

| When | Alert | Priority |
|---|---|---|
| Something becomes buyable or pre-orderable | **BUY NOW** with a buy button | high |
| An unpriced listing gets a price (Total Cards' early sign pre-orders are coming) | Priced heads-up | normal |
| Something sells out within 24h of a BUY NOW | Sold out again | low |
| A listing stops looking like it used to | Page changed, check it | high |
| A shop fails 5 times in a row | Watcher problem | normal |
| 9am, 1pm, 5pm, 9pm UK | Summary: every product's status | normal |

**Price rule:** each product can have a `shop_price`. Up to 25% over it still
alerts (and says how much over). More than 25% over is silly money: no alert,
it just shows in the summaries as over-priced. Change
`over_price_tolerance` in `watchlist.yaml` to adjust.

The first run after a fresh start records where everything stands and sends
nothing.

**If the summaries stop arriving, something is wrong** — check the
Actions tab.

## The step people skip

**Allow Telegram through your iOS Sleep Focus**, or a 3am drop won't wake you.
Settings → Focus → Sleep → Apps → allow Telegram.

ntfy is optional: set an `NTFY_TOPIC` secret to get ntfy pushes as well.

## Secrets (GitHub → Settings → Secrets → Actions)

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally `NTFY_TOPIC`. None of
them are in this repo.

## Timing

GitHub's own 5-minute schedule is best-effort and can lag or skip, so an
external free scheduler (cron-job.org) also triggers the workflow every 5
minutes via the API, using a fine-grained token limited to this repo's Actions.

## Running it by hand

From the Actions tab → **watch** → Run workflow, pick a mode:

| Mode | Does |
|---|---|
| `once` | one normal check |
| `test-notify` | sends a test push to Telegram |
| `report-now` | sends a summary now |
| `test-buy-now` | sends a 🧪 TEST BUY NOW for something really in stock, so you can try the button |

Locally:

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

## How it works

- `totalcards.py` / `magicmadhouse.py` turn a shop response into a status.
  Rate limits and challenge pages count as errors, never as "page changed".
- `state.py` holds every rule about when your phone buzzes (pure, tested).
- `run.py` does one pass; the workflow commits `state.json` back so the next
  run knows what changed. Those commits also stop GitHub disabling the
  schedule after 60 quiet days.
