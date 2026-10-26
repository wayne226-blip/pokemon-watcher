import json
from pathlib import Path

import httpx

from pokemon_watcher import collections
from pokemon_watcher.models import Settings, Status

FIX = Path(__file__).parent / "fixtures"
NOW = "2026-10-24T10:00:00+00:00"
CAPS = (
    ("ultra premium", 160.0),
    ("booster box", 170.0),
    ("elite trainer box", 60.0),
    ("mini tin", 22.0),
    ("booster bundle", 30.0),
    ("booster pack", 6.0),
    ("binder", 55.0),
    ("poster collection", 55.0),
    ("battle deck", 25.0),
    ("ex box", 35.0),
    ("tin", 35.0),
    ("tech sticker", 30.0),
    ("blister", 30.0),
)
SETTINGS = Settings(telegram_chat_id="1", request_gap_seconds=4.0, price_caps=CAPS)


def tc_pages():
    return [json.loads((FIX / f"tc_30th_page{n}.json").read_text()) for n in (1, 2)]


def parse_all(pages, shop="totalcards"):
    out = []
    for page in pages:
        out += collections.parse_collection(page, shop, "c", CAPS, NOW)
    return out


def test_tc_keeps_sealed_products_only():
    found = parse_all(tc_pages())
    titles = [p.label for p, _ in found]
    assert len(found) == 31
    assert not any("Empty" in t or t.endswith("Sticker") or t.endswith("Art Card") for t in titles)
    assert "Pokemon - 30th Celebration - Mini Tin - Day - Mew" in titles
    assert "Pokemon - 30th Celebration - Tech Sticker Collection - Lucario" in titles
    assert "Pokemon - 30th Celebration - Elite Trainer Box" in titles


def test_products_get_stable_keys_urls_and_caps():
    found = {p.handle: (p, r) for p, r in parse_all(tc_pages())}
    tin, result = found["pokemon-30th-celebration-mini-tin-mew"]
    assert tin.key == "totalcards-pokemon-30th-celebration-mini-tin-mew"
    assert tin.url == "https://totalcards.net/products/pokemon-30th-celebration-mini-tin-mew"
    assert tin.shop == "totalcards"
    assert tin.max_price == 22.0
    assert result.status is Status.PRICED
    assert result.price == 18.95


def test_cap_for_matches_first_rule():
    assert collections.cap_for("Pokemon - 30th Celebration - Mini Tin - Day - Mew", CAPS) == 22.0
    assert collections.cap_for("Pokemon - 30th Celebration - Tin - Greninja ex", CAPS) == 35.0
    assert collections.cap_for("Pokemon - 30th Celebration - Elite Trainer Box", CAPS) == 60.0
    assert collections.cap_for("Pokemon - 30th Celebration - Figure Collection - Mew", CAPS) is None


def test_zatu_collection_parses():
    page = json.loads((FIX / "zatu_30th.json").read_text())
    found = {p.handle: (p, r) for p, r in collections.parse_collection(page, "zatu", "c", CAPS, NOW)}
    assert len(found) == 20
    etb, result = found["pok-mon-tcg-30th-celebration-elite-trainer-box"]
    assert etb.url == "https://www.zatu.com/products/pok-mon-tcg-30th-celebration-elite-trainer-box"
    assert result.status is Status.AVAILABLE
    assert result.price == 169.99
    assert etb.max_price == 60.0


def test_any_available_variant_counts():
    page = {"products": [{
        "handle": "x-mini-tin", "title": "X Mini Tin", "product_type": "Tin",
        "variants": [{"price": "18.95", "available": False}, {"price": "18.95", "available": True}],
    }]}
    (_, result), = collections.parse_collection(page, "totalcards", "c", CAPS, NOW)
    assert result.status is Status.AVAILABLE


def test_collection_products_carry_a_variant_id():
    page = {"products": [{
        "handle": "x-mini-tin", "title": "X Mini Tin", "product_type": "Tin",
        "variants": [{"id": 11, "price": "18.95", "available": False}, {"id": 22, "price": "18.95", "available": True}],
    }]}
    (product, _), = collections.parse_collection(page, "totalcards", "c", CAPS, NOW)
    assert product.variant_id == 22  # the buyable one


def test_real_collection_fixture_has_variant_ids():
    found = {p.handle: p for p, _ in parse_all(tc_pages())}
    assert found["pokemon-30th-celebration-booster-pack"].variant_id == 58240898171261


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_pages_until_a_short_page():
    pages = tc_pages()
    requested, sleeps = [], []

    def handler(req):
        n = int(req.url.params["page"])
        requested.append((req.url.host, req.url.path, n))
        return httpx.Response(200, text=json.dumps(pages[n - 1]))

    found, error = collections.fetch_collection(
        _client(handler), "totalcards", "pokemon-30th-celebration", SETTINGS, sleep=sleeps.append, now=NOW
    )
    assert error is None
    assert len(found) == 31
    assert requested == [
        ("totalcards.net", "/collections/pokemon-30th-celebration/products.json", 1),
        ("totalcards.net", "/collections/pokemon-30th-celebration/products.json", 2),
    ]
    assert sleeps == [4.0]


def test_fetch_error_is_reported_not_raised():
    found, error = collections.fetch_collection(
        _client(lambda r: httpx.Response(429)), "zatu", "x", SETTINGS, sleep=lambda s: None, now=NOW
    )
    assert found == []
    assert "429" in error


def test_fetch_challenge_page_is_error():
    found, error = collections.fetch_collection(
        _client(lambda r: httpx.Response(200, text="<html>Just a moment</html>")),
        "zatu", "x", SETTINGS, sleep=lambda s: None, now=NOW,
    )
    assert found == [] and error


# --- include / exclude words per collection --------------------------------

def _page(*titles):
    return {"products": [
        {"title": t, "handle": f"h{i}", "product_type": "sealed", "variants": [{"available": False, "price": "9.99"}]}
        for i, t in enumerate(titles)
    ]}


def test_include_keeps_only_titles_with_a_listed_word():
    page = _page("Mega Evolution Delta Reign - Booster Box", "Sword & Shield - Evolving Skies - Booster Box")
    found = collections.parse_collection(page, "zatu", "c", CAPS, NOW, include=("delta reign",))
    assert [p.label for p, _ in found] == ["Mega Evolution Delta Reign - Booster Box"]


def test_exclude_drops_titles_with_a_listed_word():
    page = _page("30th Celebration - Tech Sticker Collection - Lucario", "30th Celebration - Elite Trainer Box")
    found = collections.parse_collection(page, "zatu", "c", CAPS, NOW, exclude=("sticker",))
    assert [p.label for p, _ in found] == ["30th Celebration - Elite Trainer Box"]


def test_no_filters_keeps_everything_sealed():
    page = _page("Anything - Booster Box", "Something Else - Mini Tin")
    assert len(collections.parse_collection(page, "zatu", "c", CAPS, NOW)) == 2
