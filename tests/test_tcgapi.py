import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from pokemon_watcher import tcgapi
from pokemon_watcher.models import Alert, AlertKind

FIX = Path(__file__).parent / "fixtures"
CARDS = json.loads((FIX / "tcg_me55_cards.json").read_text())["data"]
SETS = (FIX / "tcg_sets_30th.json").read_text()
SET_NAMES = ("Delta Reign", "30th Celebration", "Chaos Rising")
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)


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
        name = request.url.params["q"]
        return httpx.Response(200, text=SETS if "30th Celebration" in name else '{"data": []}')
    return httpx.Response(200, text=json.dumps({"data": CARDS}))


def full_cache():
    return tcgapi.refresh_cache({}, ("30th Celebration",), api(good_api), NOW)


# --- pure helpers ----------------------------------------------------------

def test_set_for_label_matches_product_names():
    assert tcgapi.set_for_label("30th Celebration Elite Trainer Box", SET_NAMES) == "30th Celebration"
    assert tcgapi.set_for_label("Pokemon - 30th Celebration - Mini Tin - Day - Mew", SET_NAMES) == "30th Celebration"
    assert tcgapi.set_for_label("Delta Reign Booster Bundle", SET_NAMES) == "Delta Reign"
    assert tcgapi.set_for_label("Some other box", SET_NAMES) is None


def test_chase_cards_ranked_by_rarity():
    top = tcgapi.chase_cards(CARDS, 3)
    assert [c["rarity"] for c in top] == ["Futuristic Rare", "Futuristic Rare", "Special Illustration Rare"]


# --- background cache refresh ----------------------------------------------

def test_refresh_saves_set_info_and_chase_cards():
    cache = full_cache()
    entry = cache["30th Celebration"]
    assert entry["found"] is True
    assert entry["info"] == {"id": "me55", "name": "30th Celebration", "total": 161, "releaseDate": "2026/09/16"}
    assert [c["rarity"] for c in entry["chase"]] == ["Futuristic Rare", "Futuristic Rare", "Special Illustration Rare"]
    assert entry["chase"][0]["image"].endswith("/large")


def test_refresh_picks_the_exact_set_not_the_classic_collection():
    assert full_cache()["30th Celebration"]["info"]["id"] == "me55"


def test_refresh_records_a_set_not_out_yet():
    cache = tcgapi.refresh_cache({}, ("Delta Reign",), api(good_api), NOW)
    assert cache["Delta Reign"]["found"] is False


def test_refresh_failure_keeps_old_entry_and_never_raises():
    old = full_cache()
    for failure in (lambda r: httpx.Response(502, text="error code: 502"),
                    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))):
        later = NOW + timedelta(days=2)
        cache = tcgapi.refresh_cache(old, ("30th Celebration",), api(failure), later)
        assert cache["30th Celebration"] == old["30th Celebration"]


def test_refresh_skips_fresh_entries():
    cache = full_cache()
    calls = []
    tcgapi.refresh_cache(cache, ("30th Celebration",), api(lambda r: calls.append(r) or good_api(r)),
                         NOW + timedelta(hours=1))
    assert calls == []


def test_refresh_rechecks_missing_sets_sooner():
    cache = tcgapi.refresh_cache({}, ("Delta Reign",), api(good_api), NOW)
    calls = []
    tcgapi.refresh_cache(cache, ("Delta Reign",), api(lambda r: calls.append(r) or good_api(r)),
                         NOW + timedelta(hours=7))
    assert calls  # not-out-yet sets are retried after 6h, found sets after 24h


def test_refresh_does_at_most_two_sets_per_run():
    calls = []
    tcgapi.refresh_cache({}, ("A", "B", "C", "D"), api(lambda r: calls.append(r) or good_api(r)), NOW)
    assert len([c for c in calls if c.url.path == "/v2/sets"]) == 2


# --- enriching an alert from the cache (no network) ------------------------

def test_enrich_adds_set_info_chase_cards_and_photo():
    alert = tcgapi.enrich(buy_now(), "30th Celebration Elite Trainer Box", full_cache(), SET_NAMES)
    assert alert.body.startswith("🎯 30th Celebration Elite Trainer Box")
    assert "📦 30th Celebration: 161 cards, out 16 Sep 2026" in alert.body
    assert "🌟 Chase cards" in alert.body
    assert alert.body.count("\n• ") == 3
    assert alert.image_url.endswith("/large")


def test_enrich_set_not_out_yet_says_so():
    cache = tcgapi.refresh_cache({}, ("Delta Reign",), api(good_api), NOW)
    alert = tcgapi.enrich(buy_now(), "Delta Reign Elite Trainer Box", cache, SET_NAMES)
    assert "📦 Delta Reign: card list not out yet" in alert.body
    assert alert.image_url is None


def test_enrich_without_cache_leaves_alert_alone():
    assert tcgapi.enrich(buy_now(), "30th Celebration ETB", {}, SET_NAMES).body == buy_now().body
    assert tcgapi.enrich(buy_now(), "Mystery box", full_cache(), SET_NAMES).body == buy_now().body
