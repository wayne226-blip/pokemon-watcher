"""Magic Madhouse (BigCommerce) reader.

The product page embeds BigCommerce's product JSON, including
`"instock":true|false` and `"stock":<n>`. That is the signal. The
"Add to Cart" button text is NOT: it was present on 23 Oct with stock at 0.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from .models import CheckResult, Product, Settings, Status
from .totalcards import TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

_INSTOCK = re.compile(r'"instock":(true|false)')
_STOCK = re.compile(r'"stock":(\d+)')
_PRICE = re.compile(r'"value":([\d.]+),"currency":"GBP"')


def parse_product_page(body: str, product: Product, checked_at: str) -> CheckResult:
    instock = _INSTOCK.search(body)
    if instock is None:
        return CheckResult(
            product.key, Status.UNKNOWN, error="no stock data on page", checked_at=checked_at
        )
    stock = _STOCK.search(body)
    price = _PRICE.search(body)
    available = instock.group(1) == "true" or (stock is not None and int(stock.group(1)) > 0)
    return CheckResult(
        product.key,
        Status.AVAILABLE if available else Status.UNAVAILABLE,
        price=float(price.group(1)) if price else None,
        checked_at=checked_at,
    )


def check_products(
    client: httpx.Client,
    products: list[Product],
    settings: Settings,
    sleep: Callable[[float], None] = time.sleep,
    now: str | None = None,
) -> dict[str, CheckResult]:
    results: dict[str, CheckResult] = {}
    for i, product in enumerate(products):
        if i:
            sleep(settings.request_gap_seconds)
        checked_at = now or datetime.now(timezone.utc).isoformat()
        try:
            response = client.get(
                product.url,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            log.warning("Magic Madhouse %s failed: %s", product.key, type(exc).__name__)
            results[product.key] = CheckResult(product.key, Status.ERROR, error=type(exc).__name__, checked_at=checked_at)
            continue
        if response.status_code != 200:
            log.warning("Magic Madhouse %s returned HTTP %s", product.key, response.status_code)
            results[product.key] = CheckResult(
                product.key, Status.ERROR, error=f"HTTP {response.status_code}", checked_at=checked_at
            )
            continue
        results[product.key] = parse_product_page(response.text, product, checked_at)
    return results
