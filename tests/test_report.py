from datetime import datetime, timedelta, timezone

from pokemon_watcher.models import AlertKind, Product, Settings
from pokemon_watcher.report import build_report
from pokemon_watcher.state import ProductState, WatcherState

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
SETTINGS = Settings(telegram_chat_id="1", ntfy_topic="t")
ETB = Product("tc-dr-etb", "Delta Reign Elite Trainer Box", "totalcards", "https://t/etb", "etb", shop_price=49.99)
PACK = Product("mm-dr-pack", "Delta Reign Booster Pack", "magicmadhouse", "https://m/pack", "pack", shop_price=4.29)
UPC = Product("tc-upc-day", "UPC Day", "totalcards", "https://t/upc", "upc")


def test_report_lists_every_product():
    state = WatcherState(
        products={
            "tc-dr-etb": ProductState(status="PRICED", observed="PRICED", last_price=49.95,
                                      last_check=(NOW - timedelta(minutes=4)).isoformat()),
            "mm-dr-pack": ProductState(status="UNAVAILABLE", observed="UNAVAILABLE", last_price=4.29,
                                       last_check=(NOW - timedelta(minutes=6)).isoformat()),
        },
        total_checks=288,
    )
    alert = build_report(state, [ETB, PACK, UPC], SETTINGS, NOW)
    assert alert.kind is AlertKind.REPORT
    assert alert.title == "Pokémon watcher: daily report"
    assert "Total Cards: Delta Reign Elite Trainer Box: PRICED £49.95 (4m ago)" in alert.body
    assert "Magic Madhouse: Delta Reign Booster Pack: UNAVAILABLE £4.29 (6m ago)" in alert.body
    assert "Total Cards: UPC Day: never checked" in alert.body
    assert "288" in alert.body


def test_report_marks_silly_price():
    state = WatcherState(
        products={
            "tc-dr-etb": ProductState(status="UNAVAILABLE", observed="AVAILABLE", last_price=79.95,
                                      last_check=NOW.isoformat()),
        }
    )
    alert = build_report(state, [ETB], SETTINGS, NOW)
    assert "AVAILABLE £79.95" in alert.body
    assert "[over-priced, not alerted]" in alert.body


def test_report_flags_errors():
    state = WatcherState(
        products={"tc-dr-etb": ProductState(status="UNAVAILABLE", observed="UNAVAILABLE",
                                            consecutive_errors=3, last_check=NOW.isoformat())}
    )
    assert "3 errors in a row" in build_report(state, [ETB], SETTINGS, NOW).body
