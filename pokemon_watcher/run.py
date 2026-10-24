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

    tc = [p for p in products if p.shop == "totalcards"]
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

    for p in products:
        result = results[p.key]
        new, alert = decide(state.products.get(p.key), result, p, settings, now)
        state.products[p.key] = new
        log.info("%s: %s%s", p.key, result.status.value, f" ({result.error})" if result.error else "")
        if alert:
            notifier.send(alert)
            state.events.append({"ts": stamp, "kind": alert.kind.value, "msg": alert.title})

    state.total_checks += 1

    today = now.astimezone(LONDON).date().isoformat()
    if (
        not baseline
        and now.astimezone(LONDON).hour >= settings.report_hour
        and state.last_report_date != today
    ):
        notifier.send(build_report(state, products, settings, now))
        state.last_report_date = today

    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    _setup_logging()
    sys.exit(main())
