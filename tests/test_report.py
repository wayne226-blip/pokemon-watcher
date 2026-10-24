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


def test_report_block_per_store():
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
    assert body.index("🏪 TOTAL CARDS") < body.index("🏪 ZATU") < body.index("🏪 MAGIC MADHOUSE")
    tc = body[body.index("🏪 TOTAL CARDS"):body.index("🏪 ZATU")]
    assert "⚡ Catch now:\n   • DR ETB £49.99" in tc
    assert "💤 Hiding: 1" in tc
    zatu = body[body.index("🏪 ZATU"):body.index("🏪 MAGIC MADHOUSE")]
    assert "⚡ Catch now: nothing yet" in zatu
    assert "💸 Silly money (1): Tin Mew" in zatu
    mm = body[body.index("🏪 MAGIC MADHOUSE"):]
    assert "👀 Priced (1):\n   • DR Booster Pack £4.29" in mm
    assert "288 checks so far" in body


def test_priced_list_capped_at_six():
    many = [Product(f"k{i}", f"Thing {i}", "totalcards", "u", f"h{i}") for i in range(9)]
    state = WatcherState(products={p.key: ps("PRICED", price=18.95) for p in many})
    body = build_report(state, many, SETTINGS, NOW).body
    assert "👀 Priced (9):" in body
    assert body.count("   • Thing") == 6
    assert "   • …and 3 more" in body


def test_silly_money_names_three():
    many = [Product(f"k{i}", f"Box {i}", "zatu", "u", f"h{i}", max_price=1.0) for i in range(5)]
    state = WatcherState(products={p.key: ps("UNAVAILABLE", observed="AVAILABLE", price=99.0) for p in many})
    body = build_report(state, many, SETTINGS, NOW).body
    assert "💸 Silly money (5): Box 0, Box 1, Box 2 …" in body


def test_names_are_shortened():
    long = Product("t", "Pokemon - 30th Celebration - Mini Tin - Day - Mew", "totalcards", "u", "h")
    etb = Product("e", "30th Celebration Elite Trainer Box", "totalcards", "u", "h2")
    state = WatcherState(products={"t": ps("AVAILABLE", price=18.95), "e": ps("AVAILABLE", price=52.45)})
    body = build_report(state, [long, etb], SETTINGS, NOW).body
    assert "   • 30th Tin Day - Mew £18.95" in body
    assert "   • 30th ETB £52.45" in body


def test_shops_with_nothing_watched_are_left_out():
    state = WatcherState(products={"tc-dr-etb": ps("PRICED", price=49.99)})
    body = build_report(state, [ETB], SETTINGS, NOW).body
    assert "🏪 TOTAL CARDS" in body
    assert "🏪 ZATU" not in body


def test_report_flags_problems():
    state = WatcherState(products={"tc-dr-etb": ps("UNAVAILABLE", errors=3)})
    assert "😵 Not being checked reliably: 1" in build_report(state, [ETB], SETTINGS, NOW).body


def test_report_fits_one_telegram_message():
    products = [Product(f"k{i}", f"A very long product name number {i} with extra words", "totalcards",
                        f"https://t/{i}", f"h{i}") for i in range(200)]
    state = WatcherState(products={p.key: ps("AVAILABLE", price=10.0) for p in products})
    body = build_report(state, products, SETTINGS, NOW).body
    assert len(body) <= TELEGRAM_LIMIT
    assert "more" in body
