"""Persistent state and the alerting decision rules.

`decide` is a pure function of (previous state, new result, clock). Every rule
about when Wayne's phone buzzes lives here and is unit tested — nothing about
alerting logic should leak into run.py. Ported from the Zelda watcher.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from .models import Alert, AlertKind, CheckResult, Product, Settings, Status

log = logging.getLogger(__name__)

MAX_EVENT_AGE_DAYS = 7


@dataclass
class ProductState:
    status: str = Status.UNKNOWN.value  # last status that counts for alerting
    observed: str = Status.UNKNOWN.value  # what the shop actually showed last
    last_change: str | None = None
    last_alerts: dict[str, str] = field(default_factory=dict)  # kind -> last sent
    consecutive_errors: int = 0
    consecutive_not_seen: int = 0
    last_price: float | None = None
    last_check: str | None = None
    last_buy_now: str | None = None


@dataclass
class WatcherState:
    products: dict[str, ProductState] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    started_at: str = ""
    total_checks: int = 0
    last_report_slot: str | None = None  # e.g. "2026-11-06T13"
    # Products found in shop collections, kept so they stay known (and show
    # as errors, not vanish) on a run where their collection can't be read.
    discovered: dict[str, dict] = field(default_factory=dict)
    # Pokémon TCG API set info + chase cards, refreshed in the background.
    tcg_cache: dict[str, dict] = field(default_factory=dict)


def price_verdict(
    price: float | None, shop_price: float | None, tolerance: float, max_price: float | None = None
) -> Literal["ok", "over", "silly"]:
    if max_price is not None and price is not None and price > max_price:
        return "silly"
    if price is None or shop_price is None or price <= shop_price:
        return "ok"
    if price <= shop_price * (1 + tolerance):
        return "over"
    return "silly"


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _within_cooldown(prev: ProductState, kind: AlertKind, settings: Settings, now: datetime) -> bool:
    """Each kind keeps its own clock, so an error alert can never swallow a
    BUY NOW, and a SOLD OUT AGAIN can't reset the BUY NOW cooldown when stock
    flaps 1,0,1,0 during a drop."""
    last = _parse(prev.last_alerts.get(kind.value))
    minutes = settings.buy_now_cooldown_minutes if kind is AlertKind.BUY_NOW else settings.cooldown_minutes
    return last is not None and now - last < timedelta(minutes=minutes)


def _fire(new: ProductState, alert: Alert, now: datetime) -> tuple[ProductState, Alert]:
    new.last_alerts = {**new.last_alerts, alert.kind.value: now.isoformat()}
    return new, alert


def _gbp(value: float | None) -> str:
    return "price not shown" if value is None else f"£{value:.2f}"


def _buy_now(product: Product, price: float | None, verdict: str) -> Alert:
    stock = f"🔴 IN STOCK at {product.shop_label} for {_gbp(price)}"
    if verdict == "over" and price is not None and product.shop_price is not None:
        stock += f" (£{price - product.shop_price:.2f} over shop price)"
    return Alert(
        kind=AlertKind.BUY_NOW,
        title=f"⚡ A wild {product.label} appeared!",
        body=(
            f"🎯 {product.label}\n"
            f"{stock}!\n\n"
            "🏃💨 Quick, throw your Poké Ball! Gotta catch 'em all!"
        ),
        url=product.url,
        button_label=f"🛒 Catch it at {product.shop_label}",
        priority="high",
        extra_buttons=[("⚡ Straight to checkout", product.checkout_url)] if product.checkout_url else [],
    )


def decide(
    prev: ProductState | None,
    result: CheckResult,
    product: Product,
    settings: Settings,
    now: datetime,
    announce_new: bool = False,
) -> tuple[ProductState, Alert | None]:
    if prev is None and announce_new and result.status is Status.AVAILABLE:
        # New to a running watcher (just listed, or just added to the watchlist)
        # and already buyable: that is a drop, so judge it like any restock.
        prev = ProductState()
    if prev is None:
        # Baseline: record where things stand, never alert on the first sight.
        return (
            ProductState(
                status=result.status.value,
                observed=result.status.value,
                last_change=result.checked_at,
                consecutive_errors=1 if result.status is Status.ERROR else 0,
                consecutive_not_seen=1 if result.status is Status.NOT_SEEN else 0,
                last_price=result.price,
                last_check=result.checked_at,
            ),
            None,
        )

    new = replace(prev, last_check=result.checked_at)

    if result.status is Status.ERROR:
        # Keep the last known status so an error spell can't erase a transition.
        new.consecutive_errors = prev.consecutive_errors + 1
        # Exactly once per streak: a shop blocked all night is one alert, not
        # one every cooldown window. A good read resets the streak.
        if new.consecutive_errors == settings.error_threshold:
            return _fire(
                new,
                Alert(
                    kind=AlertKind.SCRAPER_ERROR,
                    title=f"😵 {product.shop_label} used Protect!",
                    body=(
                        f"{new.consecutive_errors} failures in a row checking {product.label} "
                        f"at {product.shop_label}. Last error: {result.error}. "
                        "It is not being checked reliably."
                    ),
                    url=product.url,
                    button_label=f"Open {product.shop_label}",
                ),
                now,
            )
        return new, None

    new.consecutive_errors = 0

    if result.status is Status.NOT_SEEN:
        # Missing from search results says nothing about stock: count, never alert.
        new.consecutive_not_seen = prev.consecutive_not_seen + 1
        return new, None

    new.consecutive_not_seen = 0
    new.observed = result.status.value
    if result.price is not None:
        new.last_price = result.price

    was = prev.status
    now_status = result.status

    if now_status is Status.AVAILABLE:
        if was == Status.AVAILABLE.value:
            return new, None
        verdict = price_verdict(
            result.price, product.shop_price, settings.over_price_tolerance, product.max_price
        )
        if verdict == "silly":
            # Leave the alerting status alone so a later sane price still fires.
            return new, None
        new.status = Status.AVAILABLE.value
        new.last_change = result.checked_at
        new.last_buy_now = now.isoformat()
        if _within_cooldown(prev, AlertKind.BUY_NOW, settings, now):
            return new, None
        return _fire(new, _buy_now(product, result.price, verdict), now)

    if was != now_status.value:
        new.last_change = result.checked_at
    new.status = now_status.value

    if was == Status.AVAILABLE.value and now_status in (Status.UNAVAILABLE, Status.PRICED):
        bought_at = _parse(prev.last_buy_now)
        if (
            bought_at
            and now - bought_at < timedelta(hours=settings.sold_out_again_hours)
            and not _within_cooldown(prev, AlertKind.SOLD_OUT_AGAIN, settings, now)
        ):
            return _fire(
                new,
                Alert(
                    kind=AlertKind.SOLD_OUT_AGAIN,
                    title=f"💨 {product.label} fled!",
                    body=f"It ran away at {product.shop_label}: sold out again. It may come back…",
                    url=product.url,
                    button_label=f"Open {product.shop_label}",
                    priority="low",
                ),
                now,
            )
        return new, None

    if now_status is Status.PRICED and was not in (Status.PRICED.value, Status.AVAILABLE.value):
        if _within_cooldown(prev, AlertKind.PRICED, settings, now):
            return new, None
        return _fire(
            new,
            Alert(
                kind=AlertKind.PRICED,
                title="👀 Something's stirring in the tall grass…",
                body=(
                    f"{product.shop_label} has priced {product.label} at {_gbp(result.price)}. "
                    "Pre-orders likely soon. Get your Poké Balls ready! 🎒"
                ),
                url=product.url,
                button_label=f"Open {product.shop_label}",
            ),
            now,
        )

    if now_status is Status.UNKNOWN and was != Status.UNKNOWN.value:
        if _within_cooldown(prev, AlertKind.UNCONFIRMED, settings, now):
            return new, None
        return _fire(
            new,
            Alert(
                kind=AlertKind.UNCONFIRMED,
                title=f"❓ Who's that Pokémon? {product.shop_label}",
                body=(
                    f"{product.shop_label}'s listing for {product.label} no longer looks like it "
                    "did. Might be stock, might be a redesign. Worth a peek."
                ),
                url=product.url,
                button_label=f"Check {product.shop_label}",
                priority="high",
            ),
            now,
        )

    return new, None


_STATE_FIELDS = set(ProductState.__dataclass_fields__)


def _product_state(raw: dict) -> ProductState:
    # Ignore fields from older versions (e.g. last_alert/last_alert_kind) so a
    # code update never forces a re-baseline.
    return ProductState(**{k: v for k, v in raw.items() if k in _STATE_FIELDS})


def load_state(path: Path) -> WatcherState | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return WatcherState(
            products={k: _product_state(v) for k, v in (raw.get("products") or {}).items()},
            events=raw.get("events") or [],
            started_at=raw.get("started_at") or "",
            total_checks=raw.get("total_checks") or 0,
            discovered=raw.get("discovered") or {},
            tcg_cache=raw.get("tcg_cache") or {},
            last_report_slot=raw.get("last_report_slot"),
        )
    except (json.JSONDecodeError, OSError, TypeError, AttributeError) as exc:
        # A bad file means a silent re-baseline, never a crash and never a burst.
        log.error("state file unreadable (%s) — re-baselining", type(exc).__name__)
        return None


def save_state(path: Path, state: WatcherState) -> None:
    path = Path(path)
    cutoff = datetime.now().astimezone() - timedelta(days=MAX_EVENT_AGE_DAYS)
    state.events = [e for e in state.events if (_parse(e.get("ts")) or cutoff) >= cutoff]
    payload = {
        "products": {k: asdict(v) for k, v in state.products.items()},
        "events": state.events,
        "started_at": state.started_at,
        "total_checks": state.total_checks,
        "discovered": state.discovered,
        "tcg_cache": state.tcg_cache,
        "last_report_slot": state.last_report_slot,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic — a crash mid-write can't corrupt state
