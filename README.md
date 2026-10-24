# Pokémon Stock Watcher

Checks UK hobby card shops every ~5 minutes for new Pokémon TCG sets and pushes
an alert to Telegram and ntfy the moment something can be bought or
pre-ordered at a sane price. **It detects and alerts only — it never buys.**

Runs free on GitHub Actions, so it keeps going while the laptop is shut.

## What it watches

Edit [`watchlist.yaml`](watchlist.yaml) — never the code.

| Shop | Products | How it reads them |
|---|---|---|
| Total Cards | Delta Reign (ETB, bundle, boxes, pack, 4 blisters), 30th Celebration Ultra-Premium Collection Day + Night | 4 Shopify search requests per run |
| Magic Madhouse | Delta Reign ETB, booster box, pack | the stock data embedded in each product page |

**Not watched, and why:** Pokémon Center UK (bot challenge), Chaos Cards,
Firestorm, Smyths, Argos (all return 403 to scripts), Amazon (invite-based —
that signal arrives by email). Use Distill.io in your own Chrome for those.

## Alerts

| When | Alert | Priority |
|---|---|---|
| Something becomes buyable or pre-orderable | **BUY NOW** with a buy button | high |
| An unpriced listing gets a price (Total Cards' early sign pre-orders are coming) | Priced heads-up | normal |
| Something sells out within 24h of a BUY NOW | Sold out again | low |
| A listing stops looking like it used to | Page changed, check it | high |
| A shop fails 5 times in a row | Watcher problem | normal |
| 9am UK every day | Daily report: every product's status | normal |

**Price rule:** each product can have a `shop_price`. Up to 25% over it still
alerts (and says how much over). More than 25% over is silly money: no alert,
it just shows in the daily report as over-priced. Change
`over_price_tolerance` in `watchlist.yaml` to adjust.

The first run after a fresh start records where everything stands and sends
nothing.

**If the daily report stops arriving, something is wrong** — check the
Actions tab.

## The step people skip

**Allow Telegram and ntfy through your iOS Sleep Focus**, or a 3am drop won't
wake you. Settings → Focus → Sleep → Apps → allow Telegram and ntfy.

Subscribe to the ntfy topic (stored as the `NTFY_TOPIC` secret — it's in your
setup notes, not in this public repo) in the ntfy app.

## Secrets (GitHub → Settings → Secrets → Actions)

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NTFY_TOPIC`. None of them are in
this repo.

## Running it by hand

From the Actions tab → **watch** → Run workflow, pick a mode:

| Mode | Does |
|---|---|
| `once` | one normal check |
| `test-notify` | sends a test push to both channels |
| `report-now` | sends the daily report now |

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
