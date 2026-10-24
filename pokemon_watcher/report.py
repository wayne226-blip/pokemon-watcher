"""The summary ("Pokédex update") sent a few times a day.

Silent death is the realistic failure mode — a workflow gets disabled, a shop
changes its site. A summary that stops arriving is itself the signal.

Layout, agreed with Wayne after trying a full list (too long) and counts only
(too bare): one block per shop; everything buyable shown in full; priced and
silly-money lists inside Telegram's tap-to-expand quotes, so the message stays
short but nothing is hidden behind "…and N more". Sent as Telegram HTML.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from .models import SHOP_LABELS, Alert, AlertKind, Product, Settings, Status
from .state import WatcherState, price_verdict

TELEGRAM_LIMIT = 3900  # Telegram's hard limit is 4096; leave room for the title
NAME_PREFIXES = ("Pokemon TCG: ", "Pokémon TCG: ", "Pokemon - ", "Pokémon - ")
NAME_SHORTENINGS = (
    ("30th Celebration - ", "30th "),
    ("30th Celebration ", "30th "),
    ("Delta Reign ", "DR "),
    ("Elite Trainer Box", "ETB"),
    ("Mini Tin - ", "Tin "),
)
# If the full lists would overflow one message, retry with lists capped to these.
FALLBACK_CAPS = (None, 15, 6, 2)


def short_name(label: str) -> str:
    for prefix in NAME_PREFIXES:
        if label.startswith(prefix):
            label = label[len(prefix):]
    for long, short in NAME_SHORTENINGS:
        label = label.replace(long, short)
    return label


def _priced(name: str, price: float | None) -> str:
    text = escape(name, quote=False)
    return text if price is None else f"{text} £{price:.2f}"


def _capped(items: list[str], cap: int | None) -> list[str]:
    if cap is None or len(items) <= cap:
        return items
    return items[:cap] + [f"…and {len(items) - cap} more"]


def _expandable(items: list[str]) -> str:
    return "<blockquote expandable>" + "\n".join(f"• {i}" for i in items) + "</blockquote>"


def _shop_block(label: str, s: dict, cap: int | None) -> list[str]:
    lines = [f"<b>🏪 {escape(label.upper(), quote=False)}</b>"]
    if s["catch"]:
        lines.append("⚡ Catch now:")
        lines += [f"   • {item}" for item in _capped(s["catch"], cap)]
    else:
        lines.append("⚡ Catch now: nothing yet")
    if s["priced"]:
        lines.append(f"👀 Priced ({len(s['priced'])}):")
        lines.append(_expandable(_capped(s["priced"], cap)))
    if s["silly"]:
        lines.append(f"💸 Silly money ({len(s['silly'])}):")
        lines.append(_expandable(_capped(s["silly"], cap)))
    lines.append(f"💤 Hiding: {s['hiding']}")
    return lines


def _render(shops: dict, problems: int, total_checks: int, cap: int | None) -> str:
    lines: list[str] = []
    for key in [k for k in SHOP_LABELS if k in shops] + [k for k in shops if k not in SHOP_LABELS]:
        lines += _shop_block(SHOP_LABELS.get(key, key), shops[key], cap)
        lines.append("")
    if problems:
        lines.append(f"😵 Not being checked reliably: {problems}")
    lines.append(f"🔁 {total_checks} checks so far")
    return "\n".join(lines)


def build_report(
    state: WatcherState, products: list[Product], settings: Settings, now: datetime
) -> Alert:
    shops: dict[str, dict] = {}
    problems = 0
    for p in products:
        s = shops.setdefault(p.shop, {"catch": [], "priced": [], "silly": [], "hiding": 0})
        ps = state.products.get(p.key)
        if ps is None or ps.last_check is None:
            s["hiding"] += 1
            continue
        if ps.consecutive_errors:
            problems += 1
        name = short_name(p.label)
        if ps.observed == Status.AVAILABLE.value:
            verdict = price_verdict(ps.last_price, p.shop_price, settings.over_price_tolerance, p.max_price)
            (s["silly"] if verdict == "silly" else s["catch"]).append(_priced(name, ps.last_price))
        elif ps.observed == Status.PRICED.value:
            s["priced"].append(_priced(name, ps.last_price))
        else:
            s["hiding"] += 1

    # Full lists if they fit; otherwise cap them. Never cut the HTML mid-tag.
    for cap in FALLBACK_CAPS:
        body = _render(shops, problems, state.total_checks, cap)
        if len(body) <= TELEGRAM_LIMIT:
            break
    return Alert(kind=AlertKind.REPORT, title="📟 Pokédex update", body=body, html=True)
