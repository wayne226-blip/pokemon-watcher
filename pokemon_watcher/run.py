"""One pass over every watched product. GitHub Actions runs this on a schedule.

    python -m pokemon_watcher.run --once         check everything, alert, save state
    python -m pokemon_watcher.run --test-notify  send one test push to both channels
    python -m pokemon_watcher.run --report-now   send the daily report now
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import collections, magicmadhouse, tcgapi, totalcards
from .config import ConfigError, load_config
from .models import Alert, AlertKind, CheckResult, Product, Settings, Status
from .notify import Notifier, TokenRedactingFilter
from .report import build_report
from .state import WatcherState, _buy_now, decide, load_state, save_state

log = logging.getLogger("pokemon_watcher")
LONDON = ZoneInfo("Europe/London")

# A shop-wide block or redesign hits every product at that shop at once; send
# one alert per shop for these, not one per product.
GROUPED_KINDS = (AlertKind.SCRAPER_ERROR, AlertKind.UNCONFIRMED)


def _test_buy_now(state: WatcherState, products: list) -> tuple[Alert, str | None]:
    """A real BUY NOW, built by the real code, for something actually in stock
    — so the button opens a live page — but clearly labelled as a test."""
    candidates = list(products) + [Product(**m) for m in state.discovered.values()]
    for p in candidates:
        ps = state.products.get(p.key)
        if ps and ps.observed == Status.AVAILABLE.value:
            alert = _buy_now(p, ps.last_price, "ok")
            alert.title = f"🧪 TEST — {alert.title}"
            alert.body += "\n\n(Test alert: this item really is in stock right now. Real alerts fire the moment something comes into stock.)"
            return alert, p.label
    return Alert(
        kind=AlertKind.BUY_NOW,
        title="🧪 TEST — no wild products right now",
        body="Nothing watched is in stock at the moment, so there's no live page to test with.",
    ), None


def _heartbeat(client: httpx.Client, exit_code: int) -> None:
    """Tell healthchecks.io this run finished. It messages Wayne if the pings
    stop (GitHub, cron-job.org or the watcher died) or report a failure. Lives
    outside everything it watches, which is the whole point. Never fails a run."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        client.get(url + ("" if exit_code == 0 else "/fail"), timeout=10)
    except httpx.HTTPError as exc:
        log.warning("healthchecks.io ping failed: %s", type(exc).__name__)


def _report_slot(now: datetime, hours: tuple[int, ...]) -> str | None:
    """The latest report time already reached today, e.g. "2026-11-06T13".
    A run that misses slots (late schedule) sends one report, not a burst."""
    local = now.astimezone(LONDON)
    reached = [h for h in hours if h <= local.hour]
    return f"{local.date().isoformat()}T{max(reached):02d}" if reached else None


def _group_alert(kind: AlertKind, items: list) -> Alert:
    if len(items) == 1:
        return items[0][2]
    first_product, _, first_alert = items[0]
    shop = first_product.shop_label
    names = ", ".join(p.label for p, _, _ in items[:5])
    if len(items) > 5:
        names += f" and {len(items) - 5} more"
    if kind is AlertKind.SCRAPER_ERROR:
        title = f"😵 {shop} used Protect!"
        body = (
            f"{len(items)} products failing at {shop}. {first_alert.body.split('Last error: ', 1)[-1]}"
            f" Affected: {names}."
        )
    else:
        title = f"❓ Who's that Pokémon? {shop}"
        body = (
            f"{len(items)} {shop} listings no longer look like they did. Might be stock, "
            f"might be a redesign. Worth a peek. Affected: {names}."
        )
    return Alert(
        kind=kind,
        title=title,
        body=body,
        url=first_product.url,
        button_label=f"Check {shop}",
        priority=first_alert.priority,
    )


def _decide_and_send(
    products: list[Product],
    results: dict[str, CheckResult],
    state: WatcherState,
    settings: Settings,
    notifier,
    now: datetime,
    announce_new: bool = True,
) -> int:
    """Apply every alert rule to these results and send what fires. 0, or 2 if
    an alert reached no channel (those products are rolled back to retry).
    announce_new=False on the very first run, which only records a baseline."""
    stamp = now.isoformat()
    pending: list[tuple[Alert, list[tuple[str, object]]]] = []
    grouped: dict[tuple[str, AlertKind], list] = {}
    for p in products:
        if p.key not in results:
            continue
        result = results[p.key]
        prev = state.products.get(p.key)
        new, alert = decide(prev, result, p, settings, now, announce_new=announce_new)
        state.products[p.key] = new
        log.info("%s: %s%s", p.key, result.status.value, f" ({result.error})" if result.error else "")
        if alert is None:
            continue
        if alert.kind is AlertKind.BUY_NOW:
            alert = tcgapi.enrich(alert, p.label, state.tcg_cache, settings.tcg_sets)
        if alert.kind in GROUPED_KINDS:
            grouped.setdefault((p.shop, alert.kind), []).append((p, prev, alert))
        else:
            pending.append((alert, [(p.key, prev)]))

    for (_, kind), items in grouped.items():
        pending.append((_group_alert(kind, items), [(p.key, prev) for p, prev, _ in items]))

    exit_code = 0
    for alert, owners in pending:
        if any(notifier.send(alert).values()):
            state.events.append({"ts": stamp, "kind": alert.kind.value, "msg": alert.title})
            continue
        # Delivered nowhere: roll those products back so the next run sees the
        # same transition and tries again, and fail the run so GitHub emails.
        for key, prev in owners:
            if prev is not None:
                state.products[key] = prev
        exit_code = 2
    return exit_code


# The fast lane only acts on stock news. Errors and "not in the search results"
# are left to the 5-minute pass, so extra rounds can't trip health alerts early.
FAST_LANE_STATUSES = (Status.AVAILABLE, Status.PRICED, Status.UNAVAILABLE)


def _fast_lane_on(settings: Settings, now: datetime) -> bool:
    if not settings.fast_lane_keys or not settings.fast_lane_until:
        return False
    return now.astimezone(LONDON).date() <= date.fromisoformat(settings.fast_lane_until)


def _fast_lane(
    products: list[Product],
    state: WatcherState,
    settings: Settings,
    client: httpx.Client,
    notifier,
    now: datetime,
    started: float,
    last_checked: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    mm_fetch: magicmadhouse.Fetch,
    state_path: Path,
) -> int:
    """Re-check the must-have products about once a minute until the run has
    to end, so a drop is seen in ~1-2 minutes rather than up to 5. Rounds are
    timed from the last look at them, which was early in the main pass."""
    fast = [p for p in products if p.key in settings.fast_lane_keys]
    shopify = [p for p in fast if p.shop in totalcards.SHOPIFY_BASES]
    mm = [p for p in fast if p.shop == "magicmadhouse"]
    # One polite gap plus a second of latency per request; a slower real round
    # raises the estimate, so the run still ends before the next trigger.
    requests = len({(p.shop, p.query or p.handle) for p in shopify}) + len(mm)
    estimate = requests * (settings.request_gap_seconds + 1)
    exit_code = 0
    n = 0
    while True:
        due = max(last_checked + settings.fast_lane_every_seconds, clock())
        if due + estimate - started > settings.fast_lane_stop_after_seconds:
            break
        if due > clock():
            sleep(due - clock())
        last_checked = clock()
        n += 1
        log.info("fast lane round %d at %.0fs", n, last_checked - started)
        round_now = now + timedelta(seconds=last_checked - started)
        stamp = round_now.isoformat()
        results = totalcards.check_queries(client, shopify, settings, sleep=sleep, now=stamp)
        if mm:
            sleep(settings.request_gap_seconds)
            results.update(magicmadhouse.check_products(mm_fetch, mm, settings, sleep=sleep, now=stamp))
        news = {k: r for k, r in results.items() if r.status in FAST_LANE_STATUSES}
        exit_code = max(exit_code, _decide_and_send(fast, news, state, settings, notifier, round_now))
        save_state(state_path, state)
        estimate = max(estimate, clock() - last_checked)
        log.info("fast lane round %d done at %.0fs", n, clock() - started)
    return exit_code


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(TokenRedactingFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def main(
    argv: list[str] | None = None,
    *,
    client: httpx.Client | None = None,
    notifier=None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    mm_fetch: magicmadhouse.Fetch = magicmadhouse.curl_fetch,
    state_path: Path = Path("state.json"),
    config_path: Path = Path("watchlist.yaml"),
) -> int:
    started = clock()
    parser = argparse.ArgumentParser(prog="pokemon_watcher.run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="check every product once (default)")
    mode.add_argument("--test-notify", action="store_true", help="send a test push")
    mode.add_argument("--report-now", action="store_true", help="send the daily report now")
    mode.add_argument(
        "--test-buy-now", action="store_true",
        help="send a TEST BUY NOW for a product that is really in stock right now",
    )
    args = parser.parse_args(argv)

    try:
        settings, products = load_config(config_path)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 1

    now = now or datetime.now(timezone.utc)
    client = client or httpx.Client()
    notifier = notifier or Notifier(settings, token=os.environ.get("TELEGRAM_BOT_TOKEN"), client=client)

    if args.test_notify:
        notifier.send(
            Alert(
                kind=AlertKind.REPORT,
                title="🎒 Professor Oak here!",
                body="Test push from your Pokémon watcher. If this reached your phone, you're ready to catch 'em all.",
            )
        )
        return 0

    state = load_state(state_path)
    if args.test_buy_now:
        state = state or WatcherState()
        alert, label = _test_buy_now(state, products)
        name = tcgapi.set_for_label(label, settings.tcg_sets) if label else None
        if name and name not in state.tcg_cache:
            state.tcg_cache = tcgapi.refresh_cache(state.tcg_cache, (name,), client, now)
        if label:
            alert = tcgapi.enrich(alert, label, state.tcg_cache, settings.tcg_sets)
        notifier.send(alert)
        return 0

    if args.report_now:
        state = state or WatcherState()
        listed = {(p.shop, p.handle) for p in products}
        found = [Product(**m) for m in state.discovered.values() if (m["shop"], m["handle"]) not in listed]
        notifier.send(build_report(state, products + found, settings, now))
        return 0

    baseline = state is None
    if baseline:
        log.info("no state found — baseline run, no alerts")
        state = WatcherState(started_at=now.isoformat())
    stamp = now.isoformat()

    tc = [p for p in products if p.shop in totalcards.SHOPIFY_BASES]
    mm = [p for p in products if p.shop == "magicmadhouse"]
    results = totalcards.check_queries(client, tc, settings, sleep=sleep, now=stamp)
    priority_checked = clock()  # Shopify must-haves; the fast lane times its rounds from here
    # Stock news goes out now: collections and Magic Madhouse take another ~75 s,
    # and nothing slow may delay a BUY NOW. Misses/errors wait for the fallback below.
    early = {k: r for k, r in results.items() if r.status in FAST_LANE_STATUSES}
    exit_code = _decide_and_send(tc, early, state, settings, notifier, now, announce_new=not baseline)

    listed = {(p.shop, p.handle) for p in products}
    claimed: set[str] = set()  # a product in two collections is checked once
    for shop, handle, include, exclude in settings.collections:
        sleep(settings.request_gap_seconds)
        found, error = collections.fetch_collection(
            client, shop, handle, settings, sleep=sleep, now=stamp, include=include, exclude=exclude
        )
        known = {k: Product(**m) for k, m in state.discovered.items() if (m["shop"], m["collection"]) == (shop, handle)}
        if error:
            log.warning("%s collection %s failed: %s", shop, handle, error)
            for key in known:
                results.setdefault(key, CheckResult(key, Status.ERROR, error=error, checked_at=stamp))
        else:
            for product, result in found:
                if (product.shop, product.handle) in listed or product.key in claimed:
                    continue  # watched by hand already, or by an earlier collection
                claimed.add(product.key)
                known[product.key] = product
                state.discovered[product.key] = asdict(product)
                results[product.key] = result
            for key in known:
                results.setdefault(key, CheckResult(key, Status.NOT_SEEN, checked_at=stamp))
        have = {p.key for p in products}
        products = products + [p for k, p in known.items() if (p.shop, p.handle) not in listed and k not in have]

    # A product missing from search results for an hour gets one direct look.
    for p in tc:
        prev = state.products.get(p.key)
        if prev and prev.consecutive_not_seen >= settings.not_seen_fallback_runs:
            sleep(settings.request_gap_seconds)
            results[p.key] = totalcards.check_product_js(client, p, now=stamp)

    if mm:
        sleep(settings.request_gap_seconds)
    results.update(magicmadhouse.check_products(mm_fetch, mm, settings, sleep=sleep, now=stamp))

    rest = {k: r for k, r in results.items() if k not in early}
    exit_code = max(exit_code, _decide_and_send(products, rest, state, settings, notifier, now, announce_new=not baseline))

    state.total_checks += 1
    # After the alerts are out: a slow or broken TCG API must never delay them.
    state.tcg_cache = tcgapi.refresh_cache(state.tcg_cache, settings.tcg_sets, client, now)

    slot = _report_slot(now, settings.report_hours)
    if not baseline and slot and state.last_report_slot != slot:
        if any(notifier.send(build_report(state, products, settings, now)).values()):
            state.last_report_slot = slot
        else:
            exit_code = 2

    save_state(state_path, state)
    if not baseline and _fast_lane_on(settings, now):
        exit_code = max(exit_code, _fast_lane(
            products, state, settings, client, notifier, now, started, priority_checked,
            clock, sleep, mm_fetch, state_path,
        ))
    _heartbeat(client, exit_code)
    return exit_code


if __name__ == "__main__":
    _setup_logging()
    sys.exit(main())
