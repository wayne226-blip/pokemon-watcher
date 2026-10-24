import json
from pathlib import Path

import httpx

from pokemon_watcher import tcgapi
from pokemon_watcher.models import Alert, AlertKind

FIX = Path(__file__).parent / "fixtures"
CARDS = json.loads((FIX / "tcg_me55_cards.json").read_text())["data"]
SETS = (FIX / "tcg_sets_30th.json").read_text()
SET_NAMES = ("Delta Reign", "30th Celebration", "Chaos Rising")


def buy_now():
    return Alert(
        kind=AlertKind.BUY_NOW,
        title="⚡ A wild 30th Celebration Elite Trainer Box appeared!",
        body="🎯 30th Celebration Elite Trainer Box\n🔴 IN STOCK at Total Cards for £52.45!",
        url="https://totalcards.net/products/x",
        button_label="🛒 Catch it at Total Cards",
        priority="high",
    )


def api(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def good_api(request):
    if request.url.path == "/v2/sets":
        return httpx.Response(200, text=SETS)
    return httpx.Response(200, text=json.dumps({"data": CARDS}))


def test_set_for_label_matches_product_names():
    assert tcgapi.set_for_label("30th Celebration Elite Trainer Box", SET_NAMES) == "30th Celebration"
    assert tcgapi.set_for_label("Pokemon - 30th Celebration - Mini Tin - Day - Mew", SET_NAMES) == "30th Celebration"
    assert tcgapi.set_for_label("Delta Reign Booster Bundle", SET_NAMES) == "Delta Reign"
    assert tcgapi.set_for_label("Some other box", SET_NAMES) is None


def test_chase_cards_ranked_by_rarity():
    top = tcgapi.chase_cards(CARDS, 3)
    assert [c["rarity"] for c in top] == ["Futuristic Rare", "Futuristic Rare", "Special Illustration Rare"]


def test_enrich_adds_set_info_chase_cards_and_photo():
    alert = tcgapi.enrich(buy_now(), "30th Celebration Elite Trainer Box", api(good_api), SET_NAMES)
    assert "📦 30th Celebration: 161 cards, out 16 Sep 2026" in alert.body
    assert "🌟 Chase cards" in alert.body
    assert alert.body.count("\n• ") == 3
    assert "Futuristic Rare" in alert.body
    top = tcgapi.chase_cards(CARDS, 1)[0]
    assert alert.image_url == top["images"]["large"]
    assert alert.body.startswith("🎯 30th Celebration Elite Trainer Box")  # original text kept first


def test_enrich_picks_the_exact_set_not_the_classic_collection():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return good_api(request)

    tcgapi.enrich(buy_now(), "30th Celebration ETB", api(handler), SET_NAMES)
    assert any("set.id%3Ame55&" in u or "set.id:me55&" in u or u.endswith("set.id%3Ame55") for u in seen if "/v2/cards" in u)


def test_enrich_set_not_out_yet_says_so():
    def handler(request):
        return httpx.Response(200, text='{"data": []}')

    alert = tcgapi.enrich(buy_now(), "Delta Reign Elite Trainer Box", api(handler), SET_NAMES)
    assert "📦 Delta Reign: card list not out yet" in alert.body
    assert alert.image_url is None


def test_enrich_survives_api_errors():
    original = buy_now()
    for failure in (lambda r: httpx.Response(502, text="error code: 502"),
                    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))):
        alert = tcgapi.enrich(buy_now(), "30th Celebration ETB", api(failure), SET_NAMES)
        assert alert.body == original.body
        assert alert.image_url is None


def test_enrich_unknown_set_makes_no_request():
    calls = []
    alert = tcgapi.enrich(buy_now(), "Mystery box", api(lambda r: calls.append(r) or httpx.Response(200)), SET_NAMES)
    assert calls == []
    assert alert.body == buy_now().body
