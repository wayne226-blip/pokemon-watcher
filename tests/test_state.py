from datetime import datetime, timedelta, timezone

from pokemon_watcher.models import AlertKind, CheckResult, Product, Settings, Status
from pokemon_watcher.state import (
    ProductState,
    WatcherState,
    decide,
    load_state,
    price_verdict,
    save_state,
)

NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
SETTINGS = Settings(telegram_chat_id="1", ntfy_topic="t")
ETB = Product(
    key="tc-dr-etb",
    label="Delta Reign Elite Trainer Box",
    shop="totalcards",
    url="https://totalcards.net/products/etb",
    handle="etb",
    shop_price=49.99,
)
NO_PRICE = Product(key="x", label="UPC Day", shop="totalcards", url="https://totalcards.net/products/x", handle="x")


def res(status, price=None, at=NOW, error=None, key="tc-dr-etb"):
    return CheckResult(key, status, price=price, error=error, checked_at=at.isoformat())


def st(status="UNAVAILABLE", **kw):
    return ProductState(status=status, observed=status, **kw)


# --- price_verdict -------------------------------------------------------

def test_price_verdict_bands():
    assert price_verdict(49.99, 49.99, 0.25) == "ok"
    assert price_verdict(45.00, 49.99, 0.25) == "ok"
    assert price_verdict(54.99, 49.99, 0.25) == "over"
    assert price_verdict(62.48, 49.99, 0.25) == "over"
    assert price_verdict(79.95, 49.99, 0.25) == "silly"
    assert price_verdict(None, 49.99, 0.25) == "ok"
    assert price_verdict(79.95, None, 0.25) == "ok"


# --- baseline ------------------------------------------------------------

def test_baseline_no_alert():
    new, alert = decide(None, res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert is None
    assert new.status == "AVAILABLE"


def test_baseline_error_records_error_status():
    new, alert = decide(None, res(Status.ERROR, error="HTTP 429"), ETB, SETTINGS, NOW)
    assert alert is None
    assert new.status == "ERROR"
    assert new.consecutive_errors == 1


# --- buy now -------------------------------------------------------------

def test_unavailable_to_available_buy_now():
    new, alert = decide(st(), res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.BUY_NOW
    assert alert.priority == "high"
    assert alert.url == ETB.url
    assert alert.button_label == "🛒 Catch it at Total Cards"
    assert alert.title == "⚡ A wild Delta Reign Elite Trainer Box appeared!"
    assert "IN STOCK at Total Cards" in alert.body
    assert "Gotta catch 'em all" in alert.body
    assert "£49.99" in alert.body
    assert "over shop price" not in alert.body
    assert new.status == "AVAILABLE"
    assert new.last_buy_now == NOW.isoformat()


def test_buy_now_has_checkout_button_when_variant_known():
    product = Product(
        key="tc-dr-etb", label="Delta Reign Elite Trainer Box", shop="totalcards",
        url="https://totalcards.net/products/etb", handle="etb", variant_id=123,
    )
    _, alert = decide(st(), res(Status.AVAILABLE, 49.99), product, SETTINGS, NOW)
    assert alert.url == product.url
    assert alert.extra_buttons == [("⚡ Straight to checkout", "https://totalcards.net/cart/123:1")]


def test_buy_now_without_variant_has_no_checkout_button():
    _, alert = decide(st(), res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert.extra_buttons == []


def test_over_price_flagged():
    _, alert = decide(st(), res(Status.AVAILABLE, 54.99), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.BUY_NOW
    assert "£5.00 over shop price" in alert.body


def test_silly_price_no_alert_then_ok_price_alerts():
    new, alert = decide(st(), res(Status.AVAILABLE, 79.95), ETB, SETTINGS, NOW)
    assert alert is None
    assert new.status == "UNAVAILABLE"  # transition not used up
    assert new.observed == "AVAILABLE"
    assert new.last_price == 79.95

    later = NOW + timedelta(minutes=5)
    new2, alert2 = decide(new, res(Status.AVAILABLE, 49.99, at=later), ETB, SETTINGS, later)
    assert alert2.kind is AlertKind.BUY_NOW


def test_no_shop_price_alerts():
    _, alert = decide(st(), res(Status.AVAILABLE, 150.0, key="x"), NO_PRICE, SETTINGS, NOW)
    assert alert.kind is AlertKind.BUY_NOW
    assert "£150.00" in alert.body


def test_still_available_no_repeat():
    _, alert = decide(st("AVAILABLE"), res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert is None


# --- priced heads-up -----------------------------------------------------

def test_priced_heads_up():
    new, alert = decide(st(), res(Status.PRICED, 49.95), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.PRICED
    assert alert.priority == "default"
    assert "Total Cards has priced Delta Reign Elite Trainer Box at £49.95" in alert.body
    assert "Pre-orders likely soon" in alert.body
    assert "tall grass" in alert.title
    assert new.status == "PRICED"


def test_priced_from_not_seen_alerts():
    _, alert = decide(st("NOT_SEEN"), res(Status.PRICED, 49.95), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.PRICED


def test_priced_to_available_buy_now():
    _, alert = decide(st("PRICED"), res(Status.AVAILABLE, 49.95), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.BUY_NOW


# --- sold out again ------------------------------------------------------

def test_sold_out_again_within_24h():
    prev = st("AVAILABLE", last_buy_now=(NOW - timedelta(hours=2)).isoformat())
    new, alert = decide(prev, res(Status.UNAVAILABLE, 0.0), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.SOLD_OUT_AGAIN
    assert alert.priority == "low"


def test_sold_out_again_to_priced_counts():
    # Total Cards keeps the price after selling out: available false + price.
    prev = st("AVAILABLE", last_buy_now=(NOW - timedelta(hours=2)).isoformat())
    _, alert = decide(prev, res(Status.PRICED, 49.95), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.SOLD_OUT_AGAIN


def test_sold_out_after_24h_silent():
    prev = st("AVAILABLE", last_buy_now=(NOW - timedelta(hours=30)).isoformat())
    _, alert = decide(prev, res(Status.UNAVAILABLE, 0.0), ETB, SETTINGS, NOW)
    assert alert is None


# --- not seen ------------------------------------------------------------

def test_not_seen_never_alerts_and_counts():
    state = st("UNAVAILABLE")
    for i in range(12):
        at = NOW + timedelta(minutes=5 * i)
        state, alert = decide(state, res(Status.NOT_SEEN, at=at), ETB, SETTINGS, at)
        assert alert is None
    assert state.consecutive_not_seen == 12
    assert state.status == "UNAVAILABLE"


def test_seen_again_resets_not_seen():
    new, _ = decide(st(consecutive_not_seen=5), res(Status.UNAVAILABLE, 0.0), ETB, SETTINGS, NOW)
    assert new.consecutive_not_seen == 0


# --- errors --------------------------------------------------------------

def test_errors_alert_at_threshold_once():
    state = st()
    alerts = []
    for i in range(6):
        at = NOW + timedelta(minutes=i)
        state, alert = decide(state, res(Status.ERROR, at=at, error="HTTP 429"), ETB, SETTINGS, at)
        alerts.append(alert)
    kinds = [a.kind for a in alerts if a]
    assert kinds == [AlertKind.SCRAPER_ERROR]
    assert alerts[4] is not None  # fires on the 5th
    assert state.status == "UNAVAILABLE"


def test_error_cooldown_does_not_block_buy_now():
    prev = st(last_alerts={AlertKind.SCRAPER_ERROR.value: NOW.isoformat()}, consecutive_errors=5)
    _, alert = decide(prev, res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.BUY_NOW


def test_good_result_resets_errors():
    new, _ = decide(st(consecutive_errors=3), res(Status.UNAVAILABLE, 0.0), ETB, SETTINGS, NOW)
    assert new.consecutive_errors == 0


# --- unknown -------------------------------------------------------------

def test_unknown_alerts_high():
    _, alert = decide(st(), res(Status.UNKNOWN), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.UNCONFIRMED
    assert alert.priority == "high"


def test_unknown_twice_alerts_once():
    _, alert = decide(st("UNKNOWN"), res(Status.UNKNOWN), ETB, SETTINGS, NOW)
    assert alert is None


# --- persistence ---------------------------------------------------------

def test_missing_state_file_returns_none(tmp_path):
    assert load_state(tmp_path / "state.json") is None


def test_corrupt_state_file_returns_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert load_state(path) is None


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = WatcherState(
        products={"tc-dr-etb": st("PRICED", last_price=49.95)},
        events=[{"ts": NOW.isoformat(), "msg": "x"}],
        started_at=NOW.isoformat(),
        total_checks=7,
        last_report_slot="2026-11-06T13",
    )
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.products["tc-dr-etb"].status == "PRICED"
    assert loaded.products["tc-dr-etb"].last_price == 49.95
    assert loaded.total_checks == 7
    assert loaded.last_report_slot == "2026-11-06T13"


# --- review fixes --------------------------------------------------------

def test_errors_alert_once_per_streak():
    # Six hours of a blocked shop must be ONE alert, not one per cooldown window.
    state, alerts = st(), []
    for i in range(72):
        at = NOW + timedelta(minutes=5 * i)
        state, alert = decide(state, res(Status.ERROR, at=at, error="HTTP 429"), ETB, SETTINGS, at)
        alerts.append(alert)
    assert [a.kind for a in alerts if a] == [AlertKind.SCRAPER_ERROR]


def test_new_error_streak_alerts_again():
    state = st()
    for i in range(5):
        state, _ = decide(state, res(Status.ERROR, error="x"), ETB, SETTINGS, NOW + timedelta(minutes=i))
    state, _ = decide(state, res(Status.UNAVAILABLE, 0.0), ETB, SETTINGS, NOW + timedelta(minutes=6))
    later = NOW + timedelta(hours=3)
    alerts = []
    for i in range(5):
        at = later + timedelta(minutes=i)
        state, alert = decide(state, res(Status.ERROR, at=at, error="x"), ETB, SETTINGS, at)
        alerts.append(alert)
    assert [a.kind for a in alerts if a] == [AlertKind.SCRAPER_ERROR]


def test_flapping_stock_does_not_spam():
    # MM stock going 1,0,1,0 every 2 minutes during a drop (items held in
    # baskets): at most one BUY NOW per buy_now_cooldown_minutes, one "fled".
    state, alerts = st(), []
    seq = [Status.AVAILABLE, Status.UNAVAILABLE] * 3
    for i, s in enumerate(seq):
        at = NOW + timedelta(minutes=2 * i)
        state, alert = decide(state, res(s, 49.99, at=at), ETB, SETTINGS, at)
        alerts.append(alert)
    assert [a.kind for a in alerts if a] == [AlertKind.BUY_NOW, AlertKind.SOLD_OUT_AGAIN, AlertKind.BUY_NOW]


def test_old_state_fields_still_load(tmp_path):
    # state.json written before the per-kind change carried last_alert/last_alert_kind.
    import json as _json
    path = tmp_path / "state.json"
    path.write_text(_json.dumps({"products": {"tc-dr-etb": {
        "status": "UNAVAILABLE", "observed": "UNAVAILABLE",
        "last_alert": None, "last_alert_kind": None}}, "total_checks": 3}))
    loaded = load_state(path)
    assert loaded is not None
    assert loaded.products["tc-dr-etb"].status == "UNAVAILABLE"


# --- price caps (collection products) -----------------------------------

def test_price_verdict_respects_max_price():
    assert price_verdict(18.95, None, 0.25, max_price=22.0) == "ok"
    assert price_verdict(29.99, None, 0.25, max_price=22.0) == "silly"
    assert price_verdict(None, None, 0.25, max_price=22.0) == "ok"


def test_capped_product_over_cap_does_not_alert():
    tin = Product(key="zatu-tin", label="Mini Tin - Mew", shop="zatu", url="https://z/t", handle="t", max_price=22.0)
    _, alert = decide(st(), res(Status.AVAILABLE, 29.99, key="zatu-tin"), tin, SETTINGS, NOW)
    assert alert is None
    _, alert = decide(st(), res(Status.AVAILABLE, 18.95, key="zatu-tin"), tin, SETTINGS, NOW)
    assert alert.kind is AlertKind.BUY_NOW


# --- first sight ----------------------------------------------------------

def test_new_product_in_stock_alerts_once_the_watcher_is_running():
    new, alert = decide(None, res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW, announce_new=True)
    assert alert.kind is AlertKind.BUY_NOW
    assert new.status == "AVAILABLE"
    assert new.last_buy_now == NOW.isoformat()


def test_new_product_not_in_stock_is_just_recorded():
    for status in (Status.UNAVAILABLE, Status.PRICED, Status.NOT_SEEN, Status.ERROR):
        _, alert = decide(None, res(status, 49.99), ETB, SETTINGS, NOW, announce_new=True)
        assert alert is None


def test_first_run_never_alerts_even_in_stock():
    _, alert = decide(None, res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert is None


# --- restocks -------------------------------------------------------------

def _bought_then_sold_out(minutes_ago):
    t = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return st("UNAVAILABLE", last_alerts={"BUY_NOW": t}, last_buy_now=t)


def test_restock_after_selling_out_alerts_again():
    _, alert = decide(_bought_then_sold_out(6), res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert.kind is AlertKind.BUY_NOW


def test_stock_flicker_within_minutes_does_not_spam():
    new, alert = decide(_bought_then_sold_out(3), res(Status.AVAILABLE, 49.99), ETB, SETTINGS, NOW)
    assert alert is None
    assert new.status == "AVAILABLE"
