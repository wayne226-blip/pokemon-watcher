from pathlib import Path

import httpx

from pokemon_watcher import magicmadhouse
from pokemon_watcher.models import Product, Settings, Status

FIX = Path(__file__).parent / "fixtures"
NOW = "2026-10-24T09:00:00+00:00"
SETTINGS = Settings(telegram_chat_id="1", ntfy_topic="t", request_gap_seconds=4.0)
ETB = Product(
    key="mm-dr-etb",
    label="Delta Reign Elite Trainer Box",
    shop="magicmadhouse",
    url="https://magicmadhouse.co.uk/pokemon-me-delta-reign-elite-trainer-box-mega-rayquaza",
    handle="pokemon-me-delta-reign-elite-trainer-box-mega-rayquaza",
)
PAGE = (FIX / "mm_etb.html").read_text(encoding="utf-8", errors="ignore")


def test_fixture_out_of_stock():
    result = magicmadhouse.parse_product_page(PAGE, ETB, NOW)
    assert result.status is Status.UNAVAILABLE
    assert result.price == 49.99


def test_instock_true_is_available():
    page = PAGE.replace('"instock":false', '"instock":true')
    assert magicmadhouse.parse_product_page(page, ETB, NOW).status is Status.AVAILABLE


def test_stock_positive_is_available():
    page = PAGE.replace('"stock":0', '"stock":3')
    assert magicmadhouse.parse_product_page(page, ETB, NOW).status is Status.AVAILABLE


def test_add_to_cart_alone_is_not_available():
    page = '<html><input id="form-action-addToCart" value="Add to Cart"></html>'
    assert magicmadhouse.parse_product_page(page, ETB, NOW).status is Status.UNKNOWN


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_403_is_error():
    results = magicmadhouse.check_products(
        _client(lambda r: httpx.Response(403)), [ETB], SETTINGS, sleep=lambda s: None, now=NOW
    )
    assert results[ETB.key].status is Status.ERROR
    assert "403" in results[ETB.key].error


def test_gap_between_products():
    other = Product(key="b", label="b", shop="magicmadhouse", url="https://magicmadhouse.co.uk/b", handle="b")
    sleeps, urls = [], []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(200, text=PAGE)

    magicmadhouse.check_products(_client(handler), [ETB, other], SETTINGS, sleep=sleeps.append, now=NOW)
    assert urls == [ETB.url, other.url]
    assert sleeps == [4.0]
