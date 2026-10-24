"""The summary ("Pokédex update") sent a few times a day.

Silent death is the realistic failure mode — a workflow gets disabled, a shop
changes its site. A summary that stops arriving is itself the signal.

Kept short on purpose: one bold block per shop with a single line of counts,
naming only what's buyable now (and priced items when there are just a few).
Wayne found the full product list too much to read on a phone.
"""

from __future__ import annotations

from datetime import datetime

from .models import SHOP_LABELS, Alert, AlertKind, Product, Settings, Status
from .state import WatcherState, price_verdict

TELEGRAM_LIMIT = 3900  # Telegram's hard limit is 4096; leave room for the title
NAME_PRICED_UP_TO = 5  # list priced items by name only when a shop has this few
NAME_PREFIXES = ("Pokemon TCG: ", "Pokémon TCG: ", "Pokemon - ", "Pokémon - ")


def _short(label: str) -> str:
    for prefix in NAME_PREFIXES:
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


def _item(icon: str, p: Product, price: float | None) -> str:
    shown = "" if price is None else f" £{price:.2f}"
    return f"    {icon} {_short(p.label)}{shown}"


def build_report(
    state: WatcherState, products: list[Product], settings: Settings, now: datetime
) -> Alert:
    shops: dict[str, dict] = {}
    problems = 0
    for p in products:
        shop = shops.setdefault(p.shop, {"catch": [], "priced": [], "silly": 0, "hiding": 0})
        ps = state.products.get(p.key)
        if ps is None or ps.last_check is None:
            shop["hiding"] += 1
            continue
        if ps.consecutive_errors:
            problems += 1
        if ps.observed == Status.AVAILABLE.value:
            verdict = price_verdict(ps.last_price, p.shop_price, settings.over_price_tolerance, p.max_price)
            if verdict == "silly":
                shop["silly"] += 1
            else:
                shop["catch"].append(_item("⚡", p, ps.last_price))
        elif ps.observed == Status.PRICED.value:
            shop["priced"].append(_item("👀", p, ps.last_price))
        else:
            shop["hiding"] += 1

    lines: list[str] = []
    for key in [k for k in SHOP_LABELS if k in shops] + [k for k in shops if k not in SHOP_LABELS]:
        s = shops[key]
        lines.append(f"*🏪 {SHOP_LABELS.get(key, key)}*")
        lines.append(f"⚡ {len(s['catch'])} · 👀 {len(s['priced'])} · 💸 {s['silly']} · 💤 {s['hiding']}")
        lines += s["catch"]
        if len(s["priced"]) <= NAME_PRICED_UP_TO:
            lines += s["priced"]
        lines.append("")

    if problems:
        lines.append(f"😵 Not being checked reliably: {problems}")
    lines.append("⚡ catch now · 👀 priced · 💸 silly money · 💤 hiding")
    lines.append(f"🔁 {state.total_checks} checks so far")

    body = "\n".join(lines)
    if len(body) > TELEGRAM_LIMIT:
        body = body[: TELEGRAM_LIMIT - 20].rsplit("\n", 1)[0] + "\n…and more"
    return Alert(kind=AlertKind.REPORT, title="📟 Pokédex update", body=body)
