import json
from pathlib import Path

import httpx

from pokemon_watcher import totalcards
from pokemon_watcher.models import Product, Settings, Status

FIX = Path(__file__).parent / "fixtures"
NOW = "2026-10-24T09:00:00+00:00"
SETTINGS = Settings(telegram_chat_id="1", ntfy_topic="t", request_gap_seconds=4.0)


def product(handle, query="q", key=None):
    return Product(
        key=key or handle,
        label=handle,
        shop="totalcards",
        url=f"https://totalcards.net/products/{handle}",
        handle=handle,
        query=query,
    )


ETB = product("pokemon-mega-evolution-delta-reign-elite-trainer-box")
BUNDLE = product("pokemon-mega-evolution-delta-reign-booster-bundle")


def body(*items):
    return json.dumps({"resources": {"results": {"products": list(items)}}})


def item(handle, available=False, price="0.00"):
    return {"available": available, "price": price, "url": f"/products/{handle}?_pos=1&_sid=x"}


def test_suggest_fixture_all_unavailable():
    text = (FIX / "tc_suggest_etb.json").read_text()
    results = totalcards.parse_suggest(text, [ETB, BUNDLE], NOW)
    for key in (ETB.key, BUNDLE.key):
        assert results[key].status is Status.UNAVAILABLE
        assert results[key].price is None


def test_available_beats_zero_price():
    results = totalcards.parse_suggest(body(item(ETB.handle, True, "0.00")), [ETB], NOW)
    assert results[ETB.key].status is Status.AVAILABLE
    assert results[ETB.key].price is None  # £0.00 means "no price", never a real price


def test_priced_not_available():
    results = totalcards.parse_suggest(body(item(ETB.handle, False, "49.95")), [ETB], NOW)
    assert results[ETB.key].status is Status.PRICED
    assert results[ETB.key].price == 49.95


def test_missing_handle_is_not_seen():
    results = totalcards.parse_suggest(body(item(ETB.handle)), [ETB, BUNDLE], NOW)
    assert results[BUNDLE.key].status is Status.NOT_SEEN


def test_ignores_unrelated_hits():
    text = body(
        item("ultra-pro-pokemon-mega-evolution-delta-reign-9-pocket-portfolio", True, "11.95"),
        item(ETB.handle),
    )
    results = totalcards.parse_suggest(text, [ETB], NOW)
    assert set(results) == {ETB.key}
    assert results[ETB.key].status is Status.UNAVAILABLE


def test_handle_prefix_does_not_match():
    # booster-box must not be satisfied by booster-box-36-boosters, or vice versa
    short = product("pokemon-mega-evolution-delta-reign-booster-box")
    text = body(item("pokemon-mega-evolution-delta-reign-booster-box-36-boosters", True, "154.44"))
    assert totalcards.parse_suggest(text, [short], NOW)[short.key].status is Status.NOT_SEEN


def test_bad_shape_is_unknown():
    results = totalcards.parse_suggest('{"resources":{}}', [ETB], NOW)
    assert results[ETB.key].status is Status.UNKNOWN


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_429_is_error():
    client = _client(lambda req: httpx.Response(429))
    results = totalcards.check_queries(client, [ETB, BUNDLE], SETTINGS, sleep=lambda s: None, now=NOW)
    for key in (ETB.key, BUNDLE.key):
        assert results[key].status is Status.ERROR
        assert "429" in results[key].error


def test_html_challenge_is_error():
    client = _client(lambda req: httpx.Response(200, text="<html>challenge</html>"))
    results = totalcards.check_queries(client, [ETB], SETTINGS, sleep=lambda s: None, now=NOW)
    assert results[ETB.key].status is Status.ERROR


def test_network_failure_is_error():
    def boom(req):
        raise httpx.ConnectError("down")

    results = totalcards.check_queries(_client(boom), [ETB], SETTINGS, sleep=lambda s: None, now=NOW)
    assert results[ETB.key].status is Status.ERROR


def test_one_request_per_query_with_gap():
    seen, sleeps = [], []
    a = product("a-handle", query="first query")
    b = product("b-handle", query="first query")
    c = product("c-handle", query="second query")

    def handler(req):
        seen.append(req.url.params["q"])
        return httpx.Response(200, text=body(item("a-handle"), item("b-handle"), item("c-handle")))

    totalcards.check_queries(_client(handler), [a, b, c], SETTINGS, sleep=sleeps.append, now=NOW)
    assert seen == ["first query", "second query"]
    assert sleeps == [4.0]


def test_product_js_fixture():
    text = (FIX / "tc_product_etb.js").read_text()
    result = totalcards.parse_product_js(text, ETB, NOW)
    assert result.status is Status.UNAVAILABLE


def test_product_js_price_is_pence():
    text = json.dumps({"available": False, "price": 4995})
    result = totalcards.parse_product_js(text, ETB, NOW)
    assert result.status is Status.PRICED
    assert result.price == 49.95


def test_product_js_zero_price_is_none():
    result = totalcards.parse_product_js('{"available": true, "price": 0}', ETB, NOW)
    assert result.status is Status.AVAILABLE
    assert result.price is None


def test_each_shopify_shop_is_queried_on_its_own_site():
    hosts = []
    tc = product("pokemon-30th-celebration-elite-trainer-box", query="30th celebration elite trainer box")
    zatu = Product(
        key="zatu-etb", label="ETB", shop="zatu",
        url="https://www.zatu.com/products/pok-mon-tcg-30th-celebration-elite-trainer-box",
        handle="pok-mon-tcg-30th-celebration-elite-trainer-box", query="30th celebration elite trainer box",
    )

    def handler(req):
        hosts.append(req.url.host)
        return httpx.Response(200, text=body(item(tc.handle), item(zatu.handle)))

    results = totalcards.check_queries(_client(handler), [tc, zatu], SETTINGS, sleep=lambda s: None, now=NOW)
    assert hosts == ["totalcards.net", "www.zatu.com"]
    assert results[zatu.key].status is Status.UNAVAILABLE


def test_product_js_uses_the_products_shop():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text='{"available": false, "price": 0}')

    zatu = Product(key="z", label="z", shop="zatu", url="https://www.zatu.com/products/h", handle="h", query="q")
    totalcards.check_product_js(_client(handler), zatu, now=NOW)
    assert seen == ["https://www.zatu.com/products/h.js"]


def test_requests_alternate_between_shops():
    # Back-to-back hits on one shop trip rate limits (The Game Collection 429s
    # quickly), so spread each shop's searches out across the run.
    hosts = []
    a1 = Product(key="a1", label="a1", shop="totalcards", url="u", handle="a1", query="q1")
    a2 = Product(key="a2", label="a2", shop="totalcards", url="u", handle="a2", query="q2")
    b1 = Product(key="b1", label="b1", shop="tgc", url="u", handle="b1", query="q3")
    b2 = Product(key="b2", label="b2", shop="tgc", url="u", handle="b2", query="q4")

    def handler(req):
        hosts.append(req.url.host)
        return httpx.Response(200, text=body())

    totalcards.check_queries(_client(handler), [a1, a2, b1, b2], SETTINGS, sleep=lambda s: None, now=NOW)
    assert hosts == ["totalcards.net", "collect.thegamecollection.net", "totalcards.net", "collect.thegamecollection.net"]


def test_product_js_follows_zatu_redirect():
    # zatu.com 301s www.zatu.com/products/x.js to zatu.com/products/x.js
    def handler(request):
        if request.url.host == "www.zatu.com":
            return httpx.Response(301, headers={"location": f"https://zatu.com{request.url.path}"})
        return httpx.Response(200, json={"available": True, "price": 4999})

    product = Product(key="z", label="Z", shop="zatu", url="https://www.zatu.com/products/etb", handle="etb")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = totalcards.check_product_js(client, product, now="2026-11-06T09:00:00+00:00")
    assert result.status is Status.AVAILABLE
    assert result.price == 49.99
