"""Barcodes for watched Zatu products, for the phone's box scanner.

Zatu's product page JSON carries each box's EAN/UPC barcode and a SKU with
Pokémon's own product code (ZBG-POK1010438111). No free barcode database knows
Pokémon boxes, and the shops' search ignores barcodes, so the watcher builds its
own list here: a couple of product pages per run, after the alerts are out,
kept in state.json where the phone already reads it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from .models import Product
from .totalcards import SHOPIFY_BASES, TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

MAX_PER_RUN = 2
FOUND_REFRESH = timedelta(days=7)
MISSING_REFRESH = timedelta(days=1)


def parse_product(body: str) -> tuple[str | None, str | None]:
    """(barcode, sku) of the first variant that has a barcode, else (None, first sku)."""
    variants = json.loads(body)["product"]["variants"]
    for v in variants:
        if (v.get("barcode") or "").strip():
            return v["barcode"].strip(), v.get("sku")
    return None, variants[0].get("sku") if variants else None


def product_code(sku: str | None) -> str | None:
    """Pokémon's own product code inside a shop SKU, e.g. ZBG-POK1010438111 -> POK1010438111."""
    m = re.search(r"POK\d+", sku or "")
    return m.group(0) if m else None


def _due(entry: dict | None, now: datetime) -> bool:
    if entry is None:
        return True
    age = now - datetime.fromisoformat(entry["checked"])
    return age >= (FOUND_REFRESH if entry.get("barcode") else MISSING_REFRESH)


def refresh(
    cache: dict,
    products: list[Product],
    client: httpx.Client,
    now: datetime,
    sleep: Callable[[float], None],
    gap: float,
    max_per_run: int = MAX_PER_RUN,
) -> dict:
    """Fetch a few missing or stale barcodes, oldest first. A failed page keeps
    its old entry and is tried again on a later run."""
    cache = dict(cache)
    due = [p for p in products if p.shop == "zatu" and _due(cache.get(p.key), now)]
    due.sort(key=lambda p: cache[p.key]["checked"] if p.key in cache else "")
    for p in due[:max_per_run]:
        sleep(gap)
        try:
            response = client.get(
                f"{SHOPIFY_BASES['zatu']}/products/{p.handle}.json",
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
                follow_redirects=True,
            )
            response.raise_for_status()
            barcode, sku = parse_product(response.text)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            log.warning("barcode for %s not fetched (%s)", p.key, type(exc).__name__)
            continue
        cache[p.key] = {"barcode": barcode, "sku": sku, "checked": now.isoformat()}
    return cache
