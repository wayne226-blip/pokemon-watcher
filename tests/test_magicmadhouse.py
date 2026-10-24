from pathlib import Path

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


def test_403_is_error():
    results = magicmadhouse.check_products(
        lambda url: (403, "Just a moment..."), [ETB], SETTINGS, sleep=lambda s: None, now=NOW
    )
    assert results[ETB.key].status is Status.ERROR
    assert "403" in results[ETB.key].error


def test_fetch_failure_is_error():
    def boom(url):
        raise magicmadhouse.FetchError("curl exit 28")

    results = magicmadhouse.check_products(boom, [ETB], SETTINGS, sleep=lambda s: None, now=NOW)
    assert results[ETB.key].status is Status.ERROR
    assert "curl exit 28" in results[ETB.key].error


def test_gap_between_products():
    other = Product(key="b", label="b", shop="magicmadhouse", url="https://magicmadhouse.co.uk/b", handle="b")
    sleeps, urls = [], []

    def fetch(url):
        urls.append(url)
        return 200, PAGE

    magicmadhouse.check_products(fetch, [ETB, other], SETTINGS, sleep=sleeps.append, now=NOW)
    assert urls == [ETB.url, other.url]
    assert sleeps == [4.0]


class FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode, self.stdout = returncode, stdout


def test_curl_fetch_splits_status_from_body():
    # curl writes the body then, via -w, a trailing marker with the HTTP code.
    def runner(cmd, **kw):
        assert cmd[0] == "curl" and ETB.url in cmd
        return FakeCompleted(0, b"<html>page</html>\n__HTTP_STATUS__403")

    assert magicmadhouse.curl_fetch(ETB.url, runner=runner) == (403, "<html>page</html>")


def test_curl_fetch_nonzero_exit_raises():
    def runner(cmd, **kw):
        return FakeCompleted(28, b"")

    try:
        magicmadhouse.curl_fetch(ETB.url, runner=runner)
    except magicmadhouse.FetchError as exc:
        assert "28" in str(exc)
    else:
        raise AssertionError("expected FetchError")
