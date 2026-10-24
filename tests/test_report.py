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


def test_report_one_compact_block_per_store():
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
    assert body.index("🏪 Total Cards") < body.index("🏪 Zatu") < body.index("🏪 Magic Madhouse")
    tc = body[body.index("🏪 Total Cards"):body.index("🏪 Zatu")]
    assert "⚡ 1 · 👀 0 · 💸 0 · 💤 1" in tc
    assert "⚡ Delta Reign Elite Trainer Box £49.99" in tc  # catch-now always named
    zatu = body[body.index("🏪 Zatu"):body.index("🏪 Magic Madhouse")]
    assert "⚡ 0 · 👀 0 · 💸 1 · 💤 0" in zatu
    assert "Mini Tin" not in zatu  # silly money is a count only
    mm = body[body.index("🏪 Magic Madhouse"):]
    assert "👀 Delta Reign Booster Pack £4.29" in mm  # few priced items: named
    assert "288 checks so far" in body


def test_many_priced_items_are_just_counted():
    many = [Product(f"k{i}", f"Tin {i}", "totalcards", "u", f"h{i}") for i in range(6)]
    state = WatcherState(products={p.key: ps("PRICED", price=18.95) for p in many})
    body = build_report(state, many, SETTINGS, NOW).body
    assert "⚡ 0 · 👀 6 · 💸 0 · 💤 0" in body
    assert "Tin 3" not in body


def test_report_trims_shop_name_prefixes():
    long = Product("totalcards-tin", "Pokemon - 30th Celebration - Mini Tin - Day - Mew", "totalcards", "u", "h")
    state = WatcherState(products={"totalcards-tin": ps("AVAILABLE", price=18.95)})
    body = build_report(state, [long], SETTINGS, NOW).body
    assert "⚡ 30th Celebration - Mini Tin - Day - Mew £18.95" in body
    assert "Pokemon - " not in body


def test_shops_with_nothing_watched_are_left_out():
    state = WatcherState(products={"tc-dr-etb": ps("PRICED", price=49.99)})
    body = build_report(state, [ETB], SETTINGS, NOW).body
    assert "🏪 Total Cards" in body
    assert "🏪 Zatu" not in body


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
