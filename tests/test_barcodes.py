from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from pokemon_watcher import barcodes
from pokemon_watcher.models import Product
from pokemon_watcher.state import WatcherState, load_state, save_state

FIX = Path(__file__).parent / "fixtures"
DR_ETB = (FIX / "zatu_product_dr_etb.json").read_text()
NOW = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)


def zatu(key, handle="pokemon-tcg-mega-evolution-delta-reign-elite-trainer-box"):
    return Product(key=key, label=key, shop="zatu", url=f"https://www.zatu.com/products/{handle}", handle=handle)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(request):
    return httpx.Response(200, text=DR_ETB)


def test_parse_reads_barcode_and_sku_from_real_zatu_page():
    assert barcodes.parse_product(DR_ETB) == ("196214165809", "ZBG-POK1010438111")


def test_parse_without_barcode_gives_none():
    assert barcodes.parse_product('{"product": {"variants": [{"barcode": "", "sku": "X"}]}}') == (None, "X")


def test_product_code_strips_shop_prefixes():
    assert barcodes.product_code("ZBG-POK1010438111") == "POK1010438111"
    assert barcodes.product_code("POK1010438111") == "POK1010438111"
    assert barcodes.product_code("JPCARD-POKEMON-SV2A") is None
    assert barcodes.product_code(None) is None


def test_refresh_fetches_only_zatu_products_and_a_few_per_run():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return ok(request)

    products = [zatu(f"zatu-{i}", f"h{i}") for i in range(5)] + [
        Product(key="tc-x", label="x", shop="totalcards", url="u", handle="x")
    ]
    cache = barcodes.refresh(
        {}, products, client(handler), NOW, sleep=lambda s: None, gap=0, max_per_run=2
    )
    assert seen == ["/products/h0.json", "/products/h1.json"]
    assert cache["zatu-0"] == {"barcode": "196214165809", "sku": "ZBG-POK1010438111", "checked": NOW.isoformat()}
    assert "tc-x" not in cache


def test_refresh_skips_fresh_entries_and_redoes_stale_ones():
    fresh = {"barcode": "1", "sku": "s", "checked": (NOW - timedelta(days=1)).isoformat()}
    stale = {"barcode": "2", "sku": "s", "checked": (NOW - timedelta(days=8)).isoformat()}
    missing = {"barcode": None, "sku": "s", "checked": (NOW - timedelta(hours=25)).isoformat()}
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return ok(request)

    products = [zatu("a", "a"), zatu("b", "b"), zatu("c", "c")]
    barcodes.refresh({"a": fresh, "b": stale, "c": missing}, products, client(handler), NOW,
                     sleep=lambda s: None, gap=0, max_per_run=5)
    assert sorted(seen) == ["/products/b.json", "/products/c.json"]


def test_a_failed_fetch_keeps_the_old_entry_and_carries_on():
    old = {"barcode": "9", "sku": "s", "checked": (NOW - timedelta(days=30)).isoformat()}

    def handler(request):
        if request.url.path == "/products/bad.json":
            return httpx.Response(429)
        return ok(request)

    cache = barcodes.refresh({"bad": old}, [zatu("bad", "bad"), zatu("good", "good")], client(handler), NOW,
                             sleep=lambda s: None, gap=0, max_per_run=5)
    assert cache["bad"] == old
    assert cache["good"]["barcode"] == "196214165809"


def test_waits_between_requests():
    waits = []
    barcodes.refresh({}, [zatu("a", "a"), zatu("b", "b")], client(ok), NOW, sleep=waits.append, gap=4, max_per_run=2)
    assert waits == [4, 4]


def test_barcodes_survive_a_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = WatcherState(started_at="x")
    state.barcodes = {"zatu-a": {"barcode": "1", "sku": "s", "checked": "t"}}
    save_state(path, state)
    assert load_state(path).barcodes == state.barcodes
