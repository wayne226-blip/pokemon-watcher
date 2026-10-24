"""The summary ("Pokédex update") sent a few times a day.

Silent death is the realistic failure mode — a workflow gets disabled, a shop
changes its site. A summary that stops arriving is itself the signal.

Layout, agreed with Wayne after trying a full list (too long) and counts only
(too bare): one block per shop; everything buyable named; up to six priced
items named; a few silly-money names; a hiding count. Names are shortened.
"""

from __future__ import annotations

from datetime import datetime

from .models import SHOP_LABELS, Alert, AlertKind, Product, Settings, Status
from .state import WatcherState, price_verdict

TELEGRAM_LIMIT = 3900  # Telegram's hard limit is 4096; leave room for the title
PRICED_SHOWN = 6
SILLY_SHOWN = 3
NAME_PREFIXES = ("Pokemon TCG: ", "Pokémon TCG: ", "Pokemon - ", "Pokémon - ")
NAME_SHORTENINGS = (
    ("30th Celebration - ", "30th "),
    ("30th Celebration ", "30th "),
    ("Delta Reign ", "DR "),
    ("Elite Trainer Box", "ETB"),
    ("Mini Tin - ", "Tin "),
)


def short_name(label: str) -> str:
    for prefix in NAME_PREFIXES:
        if label.startswith(prefix):
            label = label[len(prefix):]
    for long, short in NAME_SHORTENINGS:
        label = label.replace(long, short)
    return label


def _priced(name: str, price: float | None) -> str:
    return name if price is None else f"{name} £{price:.2f}"


def _shop_block(label: str, s: dict) -> list[str]:
    lines = [f"*🏪 {label.upper()}*"]
    if s["catch"]:
        lines.append("⚡ Catch now:")
        lines += [f"   • {item}" for item in s["catch"]]
    else:
        lines.append("⚡ Catch now: nothing yet")
    if s["priced"]:
        lines.append(f"👀 Priced ({len(s['priced'])}):")
        lines += [f"   • {item}" for item in s["priced"][:PRICED_SHOWN]]
        if len(s["priced"]) > PRICED_SHOWN:
            lines.append(f"   • …and {len(s['priced']) - PRICED_SHOWN} more")
    if s["silly"]:
        names = ", ".join(s["silly"][:SILLY_SHOWN])
        more = " …" if len(s["silly"]) > SILLY_SHOWN else ""
        lines.append(f"💸 Silly money ({len(s['silly'])}): {names}{more}")
    lines.append(f"💤 Hiding: {s['hiding']}")
    return lines


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
            if verdict == "silly":
                s["silly"].append(name)
            else:
                s["catch"].append(_priced(name, ps.last_price))
        elif ps.observed == Status.PRICED.value:
            s["priced"].append(_priced(name, ps.last_price))
        else:
            s["hiding"] += 1

    lines: list[str] = []
    for key in [k for k in SHOP_LABELS if k in shops] + [k for k in shops if k not in SHOP_LABELS]:
        lines += _shop_block(SHOP_LABELS.get(key, key), shops[key])
        lines.append("")
    if problems:
        lines.append(f"😵 Not being checked reliably: {problems}")
    lines.append(f"🔁 {state.total_checks} checks so far")

    body = "\n".join(lines)
    if len(body) > TELEGRAM_LIMIT:
        body = body[: TELEGRAM_LIMIT - 20].rsplit("\n", 1)[0] + "\n…and more"
    return Alert(kind=AlertKind.REPORT, title="📟 Pokédex update", body=body)
