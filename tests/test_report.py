from datetime import datetime, timedelta, timezone

from pokemon_watcher.models import AlertKind, Product, Settings
from pokemon_watcher.report import TELEGRAM_LIMIT, build_report
from pokemon_watcher.state import ProductState, WatcherState

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
SETTINGS = Settings(telegram_chat_id="1")
ETB = Product("tc-dr-etb", "Delta Reign Elite Trainer Box", "totalcards", "https://t/etb", "etb", shop_price=49.99)
PACK = Product("mm-dr-pack", "Delta Reign Booster Pack", "magicmadhouse", "https://m/pack", "pack", shop_price=4.29)
UPC = Product("tc-upc-day", "UPC Day", "totalcards", "https://t/upc", "upc")
TIN = Product("zatu-tin", "Mini Tin - Mew", "zatu", "https://z/tin", "tin", max_price=22.0)


def ps(status, observed=None, price=None, errors=0):
    return ProductState(status=status, observed=observed or status, last_price=price,
                        consecutive_errors=errors, last_check=NOW.isoformat())


def test_report_groups_products_pokedex_style():
    state = WatcherState(
        products={
            "tc-dr-etb": ps("AVAILABLE", price=49.99),
            "mm-dr-pack": ps("PRICED", price=4.29),
            "zatu-tin": ps("UNAVAILABLE", observed="AVAILABLE", price=29.99),
        },
        total_checks=288,
    )
    alert = build_report(state, [ETB, PACK, UPC, TIN], SETTINGS, NOW)
    assert alert.kind is AlertKind.REPORT
    assert alert.title == "📟 Pokédex update"
    body = alert.body
    assert "⚡ Catch now (1)" in body
    assert "• Delta Reign Elite Trainer Box — Total Cards £49.99" in body
    assert "👀 Priced, not out yet (1)" in body
    assert "• Delta Reign Booster Pack — Magic Madhouse £4.29" in body
    assert "💸 In stock, silly money (1)" in body
    assert "• Mini Tin - Mew — Zatu £29.99" in body
    assert "💤 Still hiding: 1" in body  # UPC Day, never checked
    assert "288 checks so far" in body


def test_report_flags_problems():
    state = WatcherState(products={"tc-dr-etb": ps("UNAVAILABLE", errors=3)})
    assert "😵 Not being checked reliably: 1" in build_report(state, [ETB], SETTINGS, NOW).body


def test_report_fits_one_telegram_message():
    products = [Product(f"k{i}", f"A very long product name number {i} with extra words", "totalcards",
                        f"https://t/{i}", f"h{i}") for i in range(200)]
    state = WatcherState(products={p.key: ps("PRICED", price=10.0) for p in products})
    body = build_report(state, products, SETTINGS, NOW).body
    assert len(body) <= TELEGRAM_LIMIT
    assert "more" in body
