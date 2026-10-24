"""Set info and chase cards from the Pokémon TCG API (pokemontcg.io), added to
BUY NOW alerts.

Strictly best-effort. The API returned 500/502 on 3 of 5 lookups on 24 Oct
2026, and a BUY NOW is the one message that must never be late or lost — so
every failure here returns the alert unchanged, with short timeouts.

The API carries no prices for the 2026 Mega Evolution sets, so chase cards are
picked by rarity.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime

import httpx

from .models import Alert

log = logging.getLogger(__name__)

API = "https://api.pokemontcg.io/v2"
TIMEOUT = 8.0
CHASE_COUNT = 3
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


def enrich(alert: Alert, label: str, client: httpx.Client, set_names: tuple[str, ...]) -> Alert:
    name = set_for_label(label, set_names)
    if name is None:
        return alert
    try:
        sets = [s for s in _get(client, "/sets", {"q": f'name:"{name}"'}) if s.get("name", "").lower() == name.lower()]
        if not sets:
            return replace(alert, body=f"{alert.body}\n\n📦 {name}: card list not out yet")
        info = sets[0]
        cards = _get(
            client,
            "/cards",
            {"q": f"set.id:{info['id']}", "pageSize": 250, "select": "name,number,rarity,images"},
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        log.warning(
            "Pokémon TCG API unavailable (%s%s) — sending the alert without set info",
            type(exc).__name__, f" HTTP {status}" if status else "",
        )
        return alert

    lines = [f"📦 {info['name']}: {info.get('total', len(cards))} cards, out {_released(info.get('releaseDate'))}"]
    top = chase_cards(cards)
    if top:
        lines.append("🌟 Chase cards:")
        lines += [f"• {c['name']} ({c.get('rarity', 'rarity unknown')})" for c in top]
    image = ((top[0].get("images") or {}).get("large")) if top else None
    return replace(alert, body=f"{alert.body}\n\n" + "\n".join(lines), image_url=image)
