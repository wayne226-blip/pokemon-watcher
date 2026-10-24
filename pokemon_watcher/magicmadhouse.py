"""Magic Madhouse (BigCommerce) reader.

The product page embeds BigCommerce's product JSON, including
`"instock":true|false` and `"stock":<n>`. That is the signal. The
"Add to Cart" button text is NOT: it was present on 23 Oct with stock at 0.

Pages are fetched with curl, not httpx. Magic Madhouse sits behind
Cloudflare, which on 24 Oct served httpx a "Just a moment..." 403 from a
GitHub runner while curl from the same runner got 200 — Cloudflare
fingerprints the client library, not just the IP.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone

from .models import CheckResult, Product, Settings, Status
from .totalcards import TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

_STATUS_MARKER = "\n__HTTP_STATUS__"

Fetch = Callable[[str], tuple[int, str]]


class FetchError(Exception):
    pass


def curl_fetch(url: str, runner=subprocess.run) -> tuple[int, str]:
    """GET a page with curl. Returns (http_status, body); raises FetchError."""
    cmd = [
        "curl", "-sL", "--compressed", "-m", str(int(TIMEOUT)),
        "-A", USER_AGENT,
        "-w", _STATUS_MARKER + "%{http_code}",
        url,
    ]
    done = runner(cmd, capture_output=True, timeout=TIMEOUT + 10)
    if done.returncode != 0:
        raise FetchError(f"curl exit {done.returncode}")
    out = done.stdout.decode("utf-8", errors="ignore")
    body, _, code = out.rpartition(_STATUS_MARKER)
    return int(code), body


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
    fetch: Fetch,
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
            status, body = fetch(product.url)
        except (FetchError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            log.warning("Magic Madhouse %s failed: %s", product.key, exc)
            results[product.key] = CheckResult(product.key, Status.ERROR, error=str(exc), checked_at=checked_at)
            continue
        if status != 200:
            log.warning("Magic Madhouse %s returned HTTP %s", product.key, status)
            results[product.key] = CheckResult(
                product.key, Status.ERROR, error=f"HTTP {status}", checked_at=checked_at
            )
            continue
        results[product.key] = parse_product_page(body, product, checked_at)
    return results
