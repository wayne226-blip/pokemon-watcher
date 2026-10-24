"""Set info and chase cards from the Pokémon TCG API (pokemontcg.io), added to
BUY NOW alerts.

The API is unreliable (500/502 on most calls from GitHub on 24 Oct 2026), and a
BUY NOW is the one message that must never be late or lost. So set info is
fetched in the background into a cache kept in state.json, retried across runs,
and alerts are enriched from that cache with no network call at all.

The API carries no prices for the 2026 Mega Evolution sets, so chase cards are
picked by rarity.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta

import httpx

from .models import Alert

log = logging.getLogger(__name__)

API = "https://api.pokemontcg.io/v2"
TIMEOUT = 8.0
CHASE_COUNT = 3
MAX_SETS_PER_RUN = 2
FOUND_REFRESH = timedelta(hours=24)
MISSING_REFRESH = timedelta(hours=6)
# Best first. Anything not listed ranks below these.
RARITY_RANK = (
    "Mega Hyper Rare",
    "Futuristic Rare",
    "Hyper Rare",
    "Special Illustration Rare",
    "Mega Attack Rare",
    "Ultra Rare",
    "Illustration Rare",
    "Double Rare",
)


def set_for_label(label: str, set_names: tuple[str, ...]) -> str | None:
    """The watched set a product belongs to, by name in its title (longest wins)."""
    lowered = label.lower()
    matches = [name for name in set_names if name.lower() in lowered]
    return max(matches, key=len) if matches else None


def _rank(card: dict) -> tuple[int, int]:
    rarity = card.get("rarity")
    position = RARITY_RANK.index(rarity) if rarity in RARITY_RANK else len(RARITY_RANK)
    try:
        number = -int(str(card.get("number", "0")).split("/")[0])
    except ValueError:
        number = 0
    return position, number  # higher card numbers are the secret rares


def chase_cards(cards: list[dict], count: int = CHASE_COUNT) -> list[dict]:
    return sorted(cards, key=_rank)[:count]


def _get(client: httpx.Client, path: str, params: dict) -> list[dict]:
    headers = {}
    if os.environ.get("POKEMONTCG_API_KEY"):
        headers["X-Api-Key"] = os.environ["POKEMONTCG_API_KEY"]
    response = client.get(f"{API}{path}", params=params, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    return json.loads(response.text)["data"]


def _released(date: str | None) -> str:
    try:
        return datetime.strptime(date or "", "%Y/%m/%d").strftime("%-d %b %Y")
    except ValueError:
        return "date unknown"


def _fetch_set(client: httpx.Client, name: str) -> dict:
    sets = [s for s in _get(client, "/sets", {"q": f'name:"{name}"'}) if s.get("name", "").lower() == name.lower()]
    if not sets:
        return {"found": False}
    info = {k: sets[0].get(k) for k in ("id", "name", "total", "releaseDate")}
    cards = _get(client, "/cards", {"q": f"set.id:{info['id']}", "pageSize": 250, "select": "name,number,rarity,images"})
    chase = [
        {"name": c.get("name"), "rarity": c.get("rarity"), "image": (c.get("images") or {}).get("large")}
        for c in chase_cards(cards)
    ]
    return {"found": True, "info": info, "chase": chase}


def _due(entry: dict | None, now: datetime) -> bool:
    if entry is None:
        return True
    checked = datetime.fromisoformat(entry["checked"])
    return now - checked >= (FOUND_REFRESH if entry.get("found") else MISSING_REFRESH)


def refresh_cache(
    cache: dict, set_names: tuple[str, ...], client: httpx.Client, now: datetime, max_sets: int = MAX_SETS_PER_RUN
) -> dict:
    """Refresh a few stale sets. Runs after the shop checks, never during an
    alert, so a slow or broken API can't delay a BUY NOW. A failed set keeps its
    old entry and is simply tried again next run."""
    cache = dict(cache)
    due = [n for n in set_names if _due(cache.get(n), now)]
    due.sort(key=lambda n: cache[n]["checked"] if n in cache else "")
    for name in due[:max_sets]:
        try:
            entry = _fetch_set(client, name)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning("Pokémon TCG API: %s not refreshed (%s%s)", name, type(exc).__name__, f" HTTP {status}" if status else "")
            continue
        cache[name] = {**entry, "checked": now.isoformat()}
    return cache


def enrich(alert: Alert, label: str, cache: dict, set_names: tuple[str, ...]) -> Alert:
    """Add saved set info to a BUY NOW. Pure: no network, can't fail the alert."""
    name = set_for_label(label, set_names)
    entry = cache.get(name) if name else None
    if not entry:
        return alert
    if not entry.get("found"):
        return replace(alert, body=f"{alert.body}\n\n📦 {name}: card list not out yet")
    info = entry["info"]
    lines = [f"📦 {info['name']}: {info.get('total')} cards, out {_released(info.get('releaseDate'))}"]
    if entry.get("chase"):
        lines.append("🌟 Chase cards:")
        lines += [f"• {c['name']} ({c.get('rarity') or 'rarity unknown'})" for c in entry["chase"]]
    image = entry["chase"][0].get("image") if entry.get("chase") else None
    return replace(alert, body=f"{alert.body}\n\n" + "\n".join(lines), image_url=image)
