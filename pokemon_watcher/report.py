"""The summary ("Pokédex update") sent a few times a day.

Silent death is the realistic failure mode — a workflow gets disabled, a shop
changes its site. A summary that stops arriving is itself the signal.

With whole shop collections there can be dozens of products, so the summary
leads with what matters (buyable, priced, over-priced) and only counts the
rest, and is trimmed to fit one Telegram message.
"""

from __future__ import annotations

from datetime import datetime

from .models import Alert, AlertKind, Product, Settings, Status
from .state import WatcherState, price_verdict

TELEGRAM_LIMIT = 3900  # Telegram's hard limit is 4096; leave room for the title
MAX_LINES_PER_SECTION = 15


def _line(p: Product, price: float | None) -> str:
    shown = "" if price is None else f" £{price:.2f}"
    return f"• {p.label} — {p.shop_label}{shown}"


def _section(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    shown = lines[:MAX_LINES_PER_SECTION]
    if len(lines) > len(shown):
        shown.append(f"…and {len(lines) - len(shown)} more")
    return [f"{title} ({len(lines)})", *shown, ""]


def build_report(
    state: WatcherState, products: list[Product], settings: Settings, now: datetime
) -> Alert:
    catch, priced, silly = [], [], []
    hiding = problems = 0
    for p in products:
        ps = state.products.get(p.key)
        if ps is None or ps.last_check is None:
            hiding += 1
            continue
        if ps.consecutive_errors:
            problems += 1
        if ps.observed == Status.AVAILABLE.value:
            verdict = price_verdict(ps.last_price, p.shop_price, settings.over_price_tolerance, p.max_price)
            (silly if verdict == "silly" else catch).append(_line(p, ps.last_price))
        elif ps.observed == Status.PRICED.value:
            priced.append(_line(p, ps.last_price))
        else:
            hiding += 1

    lines = [
        *_section("⚡ Catch now", catch),
        *_section("👀 Priced, not out yet", priced),
        *_section("💸 In stock, silly money", silly),
        f"💤 Still hiding: {hiding}",
    ]
    if problems:
        lines.append(f"😵 Not being checked reliably: {problems}")
    lines += ["", f"🔁 {state.total_checks} checks so far"]

    body = "\n".join(lines)
    if len(body) > TELEGRAM_LIMIT:
        body = body[: TELEGRAM_LIMIT - 20].rsplit("\n", 1)[0] + "\n…and more"
    return Alert(kind=AlertKind.REPORT, title="📟 Pokédex update", body=body)
