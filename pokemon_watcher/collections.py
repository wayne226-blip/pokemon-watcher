"""Whole-collection reader for Shopify shops (Total Cards, Zatu).

One request reads up to 250 products, so a whole range — e.g. every 30th
Celebration product — costs one or two requests, and anything the shop adds
later is picked up automatically. Only sealed products are kept: Total Cards'
30th Celebration collection is ~80% single cards, plus empty tins and the
loose stickers and art cards sold from opened tins.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from .models import CheckResult, Product, Settings, Status
from .totalcards import SHOPIFY_BASES, TIMEOUT, USER_AGENT, _real_price, _status

log = logging.getLogger(__name__)

PAGE_SIZE = 250
MAX_PAGES = 4
SKIP_TYPES = {"single card", "empty product", "coin", "poster"}
SKIP_TITLE_WORDS = ("empty",)
SKIP_TITLE_ENDINGS = (" - sticker", " - art card")
SKIP_LANGUAGES = ("chinese", "japanese", "korean")


def cap_for(title: str, caps: tuple[tuple[str, float], ...]) -> float | None:
    """First price cap whose keyword appears in the title; None = no cap."""
    lowered = title.lower()
    for keyword, cap in caps:
        if keyword in lowered:
            return cap
    return None


def _is_sealed(raw: dict) -> bool:
    title = (raw.get("title") or "").lower()
    if (raw.get("product_type") or "").lower() in SKIP_TYPES:
        return False
    if any(w in title for w in SKIP_TITLE_WORDS + SKIP_LANGUAGES):
        return False
    return not title.endswith(SKIP_TITLE_ENDINGS)


def _wanted(title: str, include: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    lowered = title.lower()
    if include and not any(w in lowered for w in include):
        return False
    return not any(w in lowered for w in exclude)


def parse_collection(
    page: dict,
    shop: str,
    collection: str,
    caps: tuple[tuple[str, float], ...],
    checked_at: str,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> list[tuple[Product, CheckResult]]:
    base = SHOPIFY_BASES[shop]
    out = []
    for raw in page.get("products") or []:
        if not _is_sealed(raw) or not _wanted(raw.get("title") or "", include, exclude):
            continue
        variants = raw.get("variants") or [{}]
        available = any(v.get("available") for v in variants)
        prices = [float(v.get("price") or 0) for v in variants]
        price = _real_price(min(prices)) if prices else None
        handle = raw["handle"]
        product = Product(
            key=f"{shop}-{handle}",
            label=raw.get("title") or handle,
            shop=shop,
            url=f"{base}/products/{handle}",
            handle=handle,
            max_price=cap_for(raw.get("title") or "", caps),
            collection=collection,
        )
        out.append((product, CheckResult(product.key, _status(available, price), price=price, checked_at=checked_at)))
    return out


def fetch_collection(
    client: httpx.Client,
    shop: str,
    collection: str,
    settings: Settings,
    sleep: Callable[[float], None] = time.sleep,
    now: str | None = None,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> tuple[list[tuple[Product, CheckResult]], str | None]:
    """Every sealed product in a collection, or ([], error) if any page fails —
    a half-read collection would make the unread half look delisted."""
    checked_at = now or datetime.now(timezone.utc).isoformat()
    found: list[tuple[Product, CheckResult]] = []
    for n in range(1, MAX_PAGES + 1):
        if n > 1:
            sleep(settings.request_gap_seconds)
        try:
            response = client.get(
                f"{SHOPIFY_BASES[shop]}/collections/{collection}/products.json",
                params={"limit": PAGE_SIZE, "page": n},
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return [], type(exc).__name__
        if response.status_code != 200 or not response.text.lstrip().startswith("{"):
            return [], f"HTTP {response.status_code}"
        try:
            page = json.loads(response.text)
        except ValueError:
            return [], "unreadable JSON"
        found += parse_collection(page, shop, collection, settings.price_caps, checked_at, include, exclude)
        if len(page.get("products") or []) < PAGE_SIZE:
            break
    return found, None
