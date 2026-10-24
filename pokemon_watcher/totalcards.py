"""Total Cards (Shopify) reader.

One search request covers several products, which keeps us well under the
rate limit (Total Cards returned HTTP 429 after ~10 quick hits on 23 Oct).
Parsing is pure so it can be tested against saved responses.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from .models import CheckResult, Product, Settings, Status

log = logging.getLogger(__name__)

BASE = "https://totalcards.net"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)
TIMEOUT = 20.0


def _status(available: bool, price: float | None) -> Status:
    # Availability wins over price: a pre-order can open before a price shows.
    if available:
        return Status.AVAILABLE
    if price:
        return Status.PRICED
    return Status.UNAVAILABLE


def _real_price(value: float) -> float | None:
    # Total Cards lists unpriced products at £0.00; that is "no price", and
    # must never reach an alert as "available at £0.00".
    return value if value > 0 else None


def _handle_from_url(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def parse_suggest(body: str, products: list[Product], checked_at: str) -> dict[str, CheckResult]:
    try:
        hits = json.loads(body)["resources"]["results"]["products"]
        by_handle = {_handle_from_url(h["url"]): h for h in hits}
    except (ValueError, KeyError, TypeError) as exc:
        return {
            p.key: CheckResult(p.key, Status.UNKNOWN, error=f"unexpected response: {type(exc).__name__}", checked_at=checked_at)
            for p in products
        }

    results = {}
    for p in products:
        hit = by_handle.get(p.handle)
        if hit is None:
            results[p.key] = CheckResult(p.key, Status.NOT_SEEN, checked_at=checked_at)
            continue
        try:
            price = float(hit.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        price = _real_price(price)
        results[p.key] = CheckResult(
            p.key, _status(bool(hit.get("available")), price), price=price, checked_at=checked_at
        )
    return results


def parse_product_js(body: str, product: Product, checked_at: str) -> CheckResult:
    try:
        data = json.loads(body)
        price = _real_price(int(data.get("price") or 0) / 100)  # .js prices are pence
        available = bool(data["available"])
    except (ValueError, KeyError, TypeError) as exc:
        return CheckResult(product.key, Status.UNKNOWN, error=f"unexpected response: {type(exc).__name__}", checked_at=checked_at)
    return CheckResult(product.key, _status(available, price), price=price, checked_at=checked_at)


def _get(client: httpx.Client, path: str, params: dict | None = None) -> httpx.Response:
    return client.get(
        f"{BASE}{path}", params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
    )


def fetch_query(client: httpx.Client, query: str) -> httpx.Response:
    return _get(
        client,
        "/search/suggest.json",
        {"q": query, "resources[type]": "product", "resources[limit]": "10"},
    )


def fetch_product_js(client: httpx.Client, handle: str) -> httpx.Response:
    return _get(client, f"/products/{handle}.js")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_all(products: list[Product], error: str, checked_at: str) -> dict[str, CheckResult]:
    return {p.key: CheckResult(p.key, Status.ERROR, error=error, checked_at=checked_at) for p in products}


def _looks_like_json(response: httpx.Response) -> bool:
    return response.text.lstrip().startswith("{")


def check_queries(
    client: httpx.Client,
    products: list[Product],
    settings: Settings,
    sleep: Callable[[float], None] = time.sleep,
    now: str | None = None,
) -> dict[str, CheckResult]:
    """One request per distinct query, a polite gap between them."""
    groups: dict[str, list[Product]] = {}
    for p in products:
        groups.setdefault(p.query or p.handle, []).append(p)

    results: dict[str, CheckResult] = {}
    for i, (query, group) in enumerate(groups.items()):
        if i:
            sleep(settings.request_gap_seconds)
        checked_at = now or _now()
        try:
            response = fetch_query(client, query)
        except httpx.HTTPError as exc:
            log.warning("Total Cards query %r failed: %s", query, type(exc).__name__)
            results.update(_error_all(group, type(exc).__name__, checked_at))
            continue
        if response.status_code != 200 or not _looks_like_json(response):
            # A challenge page or rate limit is an ERROR, never UNKNOWN: it says
            # nothing about stock and must not fire a "page changed" alert.
            log.warning("Total Cards query %r returned HTTP %s", query, response.status_code)
            results.update(_error_all(group, f"HTTP {response.status_code}", checked_at))
            continue
        results.update(parse_suggest(response.text, group, checked_at))
    return results


def check_product_js(client: httpx.Client, product: Product, now: str | None = None) -> CheckResult:
    checked_at = now or _now()
    try:
        response = fetch_product_js(client, product.handle)
    except httpx.HTTPError as exc:
        return CheckResult(product.key, Status.ERROR, error=type(exc).__name__, checked_at=checked_at)
    if response.status_code != 200 or not _looks_like_json(response):
        return CheckResult(product.key, Status.ERROR, error=f"HTTP {response.status_code}", checked_at=checked_at)
    return parse_product_js(response.text, product, checked_at)
