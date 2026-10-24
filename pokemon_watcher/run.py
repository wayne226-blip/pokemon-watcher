"""One pass over every watched product. GitHub Actions runs this on a schedule.

    python -m pokemon_watcher.run --once         check everything, alert, save state
    python -m pokemon_watcher.run --test-notify  send one test push to both channels
    python -m pokemon_watcher.run --report-now   send the daily report now
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import magicmadhouse, totalcards
from .config import ConfigError, load_config
from .models import Alert, AlertKind
from .notify import Notifier, TokenRedactingFilter
from .report import build_report
from .state import WatcherState, decide, load_state, save_state

log = logging.getLogger("pokemon_watcher")
LONDON = ZoneInfo("Europe/London")

# A shop-wide block or redesign hits every product at that shop at once; send
# one alert per shop for these, not one per product.
GROUPED_KINDS = (AlertKind.SCRAPER_ERROR, AlertKind.UNCONFIRMED)


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
    names = ", ".join(p.label for p, _, _ in items)
    if kind is AlertKind.SCRAPER_ERROR:
        title = f"Watcher problem: {shop}"
        body = (
            f"{len(items)} products failing at {shop}. {first_alert.body.split('Last error: ', 1)[-1]}"
            f" Affected: {names}."
        )
    else:
        title = f"Page changed - {shop}"
        body = (
            f"{len(items)} {shop} listings no longer look like they did. Might be stock, "
            f"might be a redesign. Worth a check. Affected: {names}."
        )
    return Alert(
        kind=kind,
        title=title,
        body=body,
        url=first_product.url,
        button_label=f"Check {shop}",
        priority=first_alert.priority,
    )


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
    mm_fetch: magicmadhouse.Fetch = magicmadhouse.curl_fetch,
    state_path: Path = Path("state.json"),
    config_path: Path = Path("watchlist.yaml"),
) -> int:
    parser = argparse.ArgumentParser(prog="pokemon_watcher.run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="check every product once (default)")
    mode.add_argument("--test-notify", action="store_true", help="send a test push")
    mode.add_argument("--report-now", action="store_true", help="send the daily report now")
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
                title="Pokémon watcher: test",
                body="Test push. If this arrived on your phone, alerts are working.",
            )
        )
        return 0

    state = load_state(state_path)
    if args.report_now:
        notifier.send(build_report(state or WatcherState(), products, settings, now))
        return 0

    baseline = state is None
    if baseline:
        log.info("no state found — baseline run, no alerts")
        state = WatcherState(started_at=now.isoformat())
    stamp = now.isoformat()

    tc = [p for p in products if p.shop in totalcards.SHOPIFY_BASES]
    mm = [p for p in products if p.shop == "magicmadhouse"]
    results = totalcards.check_queries(client, tc, settings, sleep=sleep, now=stamp)

    # A product missing from search results for an hour gets one direct look.
    for p in tc:
        prev = state.products.get(p.key)
        if prev and prev.consecutive_not_seen >= settings.not_seen_fallback_runs:
            sleep(settings.request_gap_seconds)
            results[p.key] = totalcards.check_product_js(client, p, now=stamp)

    if mm:
        sleep(settings.request_gap_seconds)
    results.update(magicmadhouse.check_products(mm_fetch, mm, settings, sleep=sleep, now=stamp))

    pending: list[tuple[Alert, list[tuple[str, object]]]] = []
    grouped: dict[tuple[str, AlertKind], list] = {}
    for p in products:
        result = results[p.key]
        prev = state.products.get(p.key)
        new, alert = decide(prev, result, p, settings, now)
        state.products[p.key] = new
        log.info("%s: %s%s", p.key, result.status.value, f" ({result.error})" if result.error else "")
        if alert is None:
            continue
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

    state.total_checks += 1

    slot = _report_slot(now, settings.report_hours)
    if not baseline and slot and state.last_report_slot != slot:
        if any(notifier.send(build_report(state, products, settings, now)).values()):
            state.last_report_slot = slot
        else:
            exit_code = 2

    save_state(state_path, state)
    return exit_code


if __name__ == "__main__":
    _setup_logging()
    sys.exit(main())
