"""Daily status report.

Silent death is the realistic failure mode — a workflow gets disabled, a shop
changes its site. A report that stops arriving is itself the signal.
"""

from __future__ import annotations

from datetime import datetime

from .models import Alert, AlertKind, Product, Settings, Status
from .state import WatcherState, price_verdict


def _age(iso: str | None, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        then = None
    if then is None:
        return "unknown"
    seconds = int((now - then).total_seconds())
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def build_report(
    state: WatcherState, products: list[Product], settings: Settings, now: datetime
) -> Alert:
    lines = []
    for p in products:
        ps = state.products.get(p.key)
        prefix = f"{p.shop_label}: {p.label}:"
        if ps is None or ps.last_check is None:
            lines.append(f"{prefix} never checked")
            continue
        price = "" if ps.last_price is None else f" £{ps.last_price:.2f}"
        line = f"{prefix} {ps.observed}{price} ({_age(ps.last_check, now)})"
        if (
            ps.observed == Status.AVAILABLE.value
            and ps.status != Status.AVAILABLE.value
            and price_verdict(ps.last_price, p.shop_price, settings.over_price_tolerance) == "silly"
        ):
            line += " [over-priced, not alerted]"
        if ps.consecutive_errors:
            line += f" [{ps.consecutive_errors} errors in a row]"
        lines.append(line)

    lines.append("")
    lines.append(f"{state.total_checks} checks so far.")
    return Alert(
        kind=AlertKind.REPORT,
        title="Pokémon watcher: summary",
        body="\n".join(lines),
    )
