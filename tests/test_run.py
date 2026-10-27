import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from pokemon_watcher import run
from pokemon_watcher.models import AlertKind
from pokemon_watcher.state import load_state, save_state

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent
MORNING = datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc)   # 07:00 London (GMT)
AFTER_NINE = datetime(2026, 11, 6, 9, 30, tzinfo=timezone.utc)
MM_PAGE = (FIX / "mm_etb.html").read_text(encoding="utf-8", errors="ignore")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return {"telegram": True, "ntfy": True}


TC_PAGES = [json.loads((FIX / f"tc_30th_page{n}.json").read_text()) for n in (1, 2)]
ZATU_PAGE = json.loads((FIX / "zatu_30th.json").read_text())


def _flip(page, handle, available, price):
    page = json.loads(json.dumps(page))
    for p in page["products"]:
        if p["handle"] == handle:
            for v in p["variants"]:
                v["available"], v["price"] = available, price
    return page


def shop_handler(etb_available=False, js_requests=None, tc_pages=None, zatu_page=None, collections_down=False):
    """All TC queries return every watched handle; MM returns the fixture page."""
    from pokemon_watcher.config import load_config

    _, products = load_config(ROOT / "watchlist.yaml", {"TELEGRAM_CHAT_ID": "1", "NTFY_TOPIC": "t"})
    tc = [p for p in products if p.shop in ("totalcards", "zatu")]

    def handler(request):
        url = str(request.url)
        if request.url.host == "api.pokemontcg.io":
            if request.url.path == "/v2/sets":
                return httpx.Response(200, text=(FIX / "tcg_sets_30th.json").read_text())
            return httpx.Response(200, text=(FIX / "tcg_me55_cards.json").read_text())
        if "/collections/" in url:
            if collections_down:
                return httpx.Response(429)
            if request.url.host == "www.zatu.com":
                return httpx.Response(200, text=json.dumps(zatu_page or ZATU_PAGE))
            pages = tc_pages or TC_PAGES
            return httpx.Response(200, text=json.dumps(pages[int(request.url.params["page"]) - 1]))
        if "suggest.json" in url and request.url.host == "collect.thegamecollection.net":
            return httpx.Response(200, text=json.dumps({"resources": {"results": {"products": []}}}))
        if "suggest.json" in url:
            items = [
                {
                    "available": etb_available and p.key == "tc-dr-etb",
                    "price": "49.99" if etb_available and p.key == "tc-dr-etb" else "0.00",
                    "url": f"/products/{p.handle}?_pos=1",
                }
                for p in tc
            ]
            return httpx.Response(200, text=json.dumps({"resources": {"results": {"products": items}}}))
        if url.endswith(".js"):
            if js_requests is not None:
                js_requests.append(url)
            return httpx.Response(200, text=json.dumps({"available": False, "price": 0}))
        return httpx.Response(200, text=MM_PAGE)

    return handler


class FakeClock:
    """Sleeping moves time on, so the fast lane's timing can be checked."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append((self.t, seconds))
        self.t += seconds


def _hiding(handle, handler):
    """Drop one handle from every search result, as if it fell out of the top 10."""
    def wrapped(request):
        response = handler(request)
        if "suggest.json" not in str(request.url):
            return response
        data = json.loads(response.text)
        products = data["resources"]["results"]["products"]
        data["resources"]["results"]["products"] = [p for p in products if f"/products/{handle}?" not in p["url"]]
        return httpx.Response(200, text=json.dumps(data))
    return wrapped


def _main(tmp_path, handler, now, notifier, argv=None, clock=None):
    clock = clock or FakeClock()
    return run.main(
        argv or ["--once"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        notifier=notifier,
        now=now,
        sleep=clock.sleep,
        clock=clock,
        mm_fetch=lambda url: (lambda r: (r.status_code, r.text))(handler(httpx.Request("GET", url))),
        state_path=tmp_path / "state.json",
        config_path=ROOT / "watchlist.yaml",
    )


def test_first_run_is_silent_and_writes_state(tmp_path):
    notifier = FakeNotifier()
    assert _main(tmp_path, shop_handler(etb_available=True), MORNING, notifier) == 0
    assert notifier.sent == []
    state = load_state(tmp_path / "state.json")
    # 37 listed + 31 Total Cards + 20 Zatu sealed 30th products, minus the 4
    # already listed by hand (TC ETB, UPC Day, UPC Night; Zatu ETB).
    assert len(state.products) == 99  # +1: Final Fantasy TCG Dissidia set, +1: TGC 30th battle deck
    assert "totalcards-pokemon-30th-celebration-mini-tin-mew" in state.products
    assert state.total_checks == 1


def test_unreadable_state_warns_and_still_alerts(tmp_path):
    (tmp_path / "state.json").write_text("{ not json <<<<<<< merge conflict")
    notifier = FakeNotifier()
    code = _main(tmp_path, shop_handler(etb_available=True), MORNING, notifier)
    kinds = [a.kind for a in notifier.sent]
    assert kinds[0] is AlertKind.SCRAPER_ERROR
    assert "state" in notifier.sent[0].body.lower()
    # A drop that happened while state was broken must not be swallowed.
    assert AlertKind.BUY_NOW in kinds
    assert code == 2  # fail the run so GitHub emails as well
    assert load_state(tmp_path / "state.json") is not None  # rebuilt for next time


def test_missing_state_is_a_quiet_first_run(tmp_path):
    notifier = FakeNotifier()
    assert _main(tmp_path, shop_handler(etb_available=True), MORNING, notifier) == 0
    assert notifier.sent == []


def test_second_run_available_sends_buy_now(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    _main(tmp_path, shop_handler(etb_available=True), MORNING, notifier)
    kinds = [a.kind for a in notifier.sent]
    assert kinds == [AlertKind.BUY_NOW]
    assert "Delta Reign Elite Trainer Box" in notifier.sent[0].title


def test_product_new_to_a_running_watcher_alerts_if_in_stock(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    state = load_state(tmp_path / "state.json")
    del state.products["tc-dr-etb"]  # e.g. just added to watchlist.yaml
    save_state(tmp_path / "state.json", state)
    _main(tmp_path, shop_handler(etb_available=True), MORNING, notifier)
    assert [a.kind for a in notifier.sent] == [AlertKind.BUY_NOW]


def test_shopify_buy_now_is_sent_before_the_slow_reads(tmp_path):
    # Collections and Magic Madhouse take ~75 s; a BUY NOW must not wait for them.
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    log = []
    live = shop_handler(etb_available=True)

    def handler(request):
        log.append("request " + ("slow" if "/collections/" in str(request.url) or "magicmadhouse" in str(request.url) else "search"))
        return live(request)

    class OrderedNotifier(FakeNotifier):
        def send(self, alert):
            log.append(f"send {alert.kind.value}")
            return super().send(alert)

    _main(tmp_path, handler, MORNING, OrderedNotifier())
    first_slow = log.index("request slow")
    assert "send BUY_NOW" in log[:first_slow]


def test_not_seen_fallback_fetches_js_after_12(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    state = load_state(tmp_path / "state.json")
    state.products["tc-upc-day"].consecutive_not_seen = 12
    save_state(tmp_path / "state.json", state)

    js = []
    _main(tmp_path, _hiding("pokemon-30th-celebration-ultra-premium-collection-day", shop_handler(js_requests=js)), MORNING, notifier)
    assert js == ["https://totalcards.net/products/pokemon-30th-celebration-ultra-premium-collection-day.js"]


def _js_after_missing(tmp_path, key, handle, not_seen):
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    state = load_state(tmp_path / "state.json")
    state.products[key].consecutive_not_seen = not_seen
    save_state(tmp_path / "state.json", state)
    js = []
    _main(tmp_path, _hiding(handle, shop_handler(js_requests=js)), MORNING, FakeNotifier())
    return js


def test_must_have_gets_direct_check_after_one_missed_run(tmp_path):
    js = _js_after_missing(tmp_path, "tc-dr-etb", "pokemon-mega-evolution-delta-reign-elite-trainer-box", 1)
    assert js == ["https://totalcards.net/products/pokemon-mega-evolution-delta-reign-elite-trainer-box.js"]


def test_other_products_still_wait_for_the_full_fallback(tmp_path):
    js = _js_after_missing(tmp_path, "tc-dr-bundle", "pokemon-mega-evolution-delta-reign-booster-bundle", 1)
    assert js == []


def test_tgc_is_never_direct_checked(tmp_path):
    # TGC's .js feed 429s scripts: a direct check would only manufacture errors.
    js = _js_after_missing(tmp_path, "tgc-dr-etb", "pokemon-tcg-mega-evolution-delta-reign-elite-trainer-box", 12)
    assert not [u for u in js if "thegamecollection" in u]


def test_product_back_in_search_skips_the_fallback(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    state = load_state(tmp_path / "state.json")
    state.products["tc-upc-day"].consecutive_not_seen = 12
    save_state(tmp_path / "state.json", state)
    js = []
    _main(tmp_path, shop_handler(js_requests=js), MORNING, notifier)
    assert js == []


def test_report_sent_once_per_slot(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)      # baseline, before 9
    _main(tmp_path, shop_handler(), AFTER_NINE, notifier)   # 9am report
    _main(tmp_path, shop_handler(), AFTER_NINE, notifier)   # not again
    assert [a.kind for a in notifier.sent] == [AlertKind.REPORT]


def test_three_reports_a_day(tmp_path):
    # 9am, 1pm, 9pm London (5pm dropped). November = GMT, so London hour == UTC hour.
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)  # baseline 07:00
    for hh, mm in [(9, 30), (9, 35), (12, 0), (13, 5), (13, 10), (17, 0), (21, 10), (21, 15), (23, 50)]:
        at = datetime(2026, 11, 6, hh, mm, tzinfo=timezone.utc)
        _main(tmp_path, shop_handler(), at, notifier)
    assert [a.kind for a in notifier.sent] == [AlertKind.REPORT] * 3  # nothing at 17:00


def test_missed_slots_send_one_report_not_a_burst(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    _main(tmp_path, shop_handler(), datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc), notifier)
    assert [a.kind for a in notifier.sent] == [AlertKind.REPORT]


def test_shop_down_does_not_crash(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    assert _main(tmp_path, lambda r: httpx.Response(503), MORNING, notifier) == 0
    assert notifier.sent == []


def test_test_notify_sends_one_alert(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier, argv=["--test-notify"])
    assert len(notifier.sent) == 1
    assert not (tmp_path / "state.json").exists()


def test_missing_config_secret_exits_1(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert _main(tmp_path, shop_handler(), MORNING, FakeNotifier()) == 1


# --- review fixes --------------------------------------------------------

class DeadNotifier(FakeNotifier):
    def send(self, alert):
        self.sent.append(alert)
        return {"telegram": False, "ntfy": False}


def test_blocked_shops_send_one_alert_per_shop(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    for _ in range(8):
        _main(tmp_path, lambda r: httpx.Response(429), MORNING, notifier)
    errors = [a for a in notifier.sent if a.kind is AlertKind.SCRAPER_ERROR]
    assert len(errors) == 4  # one each: Total Cards, Zatu, The Game Collection, Magic Madhouse
    tc = next(a for a in errors if "Total Cards" in a.title)
    assert "40 products" in tc.body  # 12 listed + 28 from the 30th collection


def test_undelivered_alert_is_retried_and_run_fails(tmp_path):
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())  # baseline
    assert _main(tmp_path, shop_handler(etb_available=True), MORNING, DeadNotifier()) == 2
    good = FakeNotifier()
    _main(tmp_path, shop_handler(etb_available=True), MORNING, good)
    assert [a.kind for a in good.sent] == [AlertKind.BUY_NOW]


def test_workflow_checks_out_branch_tip_and_keeps_own_state():
    wf = (ROOT / ".github" / "workflows" / "watch.yml").read_text()
    assert "ref: main" in wf           # never read state.json from a stale trigger sha
    assert "-X theirs" in wf           # on a rebase conflict, this run's state wins
    assert "${{ inputs.mode" not in wf.split("run: python")[1].split("\n")[0]



# --- whole collections ----------------------------------------------------

MEW = "pokemon-30th-celebration-mini-tin-mew"


def test_mini_tin_in_stock_sends_buy_now_with_link(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    pages = [_flip(TC_PAGES[0], MEW, True, "18.95"), TC_PAGES[1]]
    _main(tmp_path, shop_handler(tc_pages=pages), MORNING, notifier)
    (alert,) = notifier.sent
    assert alert.kind is AlertKind.BUY_NOW
    assert "Mini Tin - Day - Mew" in alert.body
    assert "📦 30th Celebration: 161 cards" in alert.body
    assert "🌟 Chase cards" in alert.body
    assert alert.image_url
    assert alert.url == f"https://totalcards.net/products/{MEW}"


def test_zatu_mini_tin_alerts_at_any_price(tmp_path):
    # Price caps are off (26 Oct): a £29.99 mini tin still gets a BUY NOW.
    notifier = FakeNotifier()
    page = _flip(ZATU_PAGE, "pok-mon-tcg-30th-celebration-mini-tin-mew", False, "29.99")
    _main(tmp_path, shop_handler(zatu_page=page), MORNING, notifier)
    page = _flip(ZATU_PAGE, "pok-mon-tcg-30th-celebration-mini-tin-mew", True, "29.99")
    _main(tmp_path, shop_handler(zatu_page=page), MORNING, notifier)
    assert [a.kind for a in notifier.sent] == [AlertKind.BUY_NOW]


def test_collection_outage_keeps_products_as_errors(tmp_path):
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    _main(tmp_path, shop_handler(collections_down=True), MORNING, FakeNotifier())
    state = load_state(tmp_path / "state.json")
    assert state.products[f"totalcards-{MEW}"].consecutive_errors == 1
    assert state.products[f"totalcards-{MEW}"].consecutive_not_seen == 0


def test_delisted_collection_product_is_not_seen(tmp_path):
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    page = json.loads(json.dumps(TC_PAGES[0]))
    page["products"] = [p for p in page["products"] if p["handle"] != MEW]
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(tc_pages=[page, TC_PAGES[1]]), MORNING, notifier)
    state = load_state(tmp_path / "state.json")
    assert state.products[f"totalcards-{MEW}"].consecutive_not_seen == 1
    assert notifier.sent == []


def test_report_now_includes_collection_products(tmp_path):
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier, argv=["--report-now"])
    (report,) = notifier.sent
    # No price limits: Zatu's marked-up 30th stock is listed as catchable.
    zatu = report.body[report.body.index("🏪 ZATU"):report.body.index("🏪 THE GAME COLLECTION")]
    assert "Silly money" not in zatu
    assert "30th Sylveon ex Tin £68.99" in zatu


def test_test_buy_now_uses_a_real_in_stock_product(tmp_path):
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    notifier = FakeNotifier()
    assert _main(tmp_path, shop_handler(), MORNING, notifier, argv=["--test-buy-now"]) == 0
    (alert,) = notifier.sent
    assert alert.kind is AlertKind.BUY_NOW
    assert alert.title.startswith("🧪 TEST")
    assert "🌟 Chase cards" in alert.body
    assert "A wild" in alert.title
    assert alert.button_label.startswith("🛒 Catch it at")
    # The button must open a product that really is in stock right now.
    state = load_state(tmp_path / "state.json")
    key = next(k for k, m in state.discovered.items() if alert.url == m["url"])
    assert state.products[key].observed == "AVAILABLE"


def test_test_buy_now_without_stock_says_so(tmp_path):
    notifier = FakeNotifier()
    assert _main(tmp_path, shop_handler(), MORNING, notifier, argv=["--test-buy-now"]) == 0
    (alert,) = notifier.sent
    assert "nothing" in alert.body.lower()


def test_tcg_api_outage_never_blocks_a_buy_now(tmp_path):
    inner = shop_handler()

    def broken_api(request):
        if request.url.host == "api.pokemontcg.io":
            return httpx.Response(502, text="error code: 502")
        return inner(request)

    _main(tmp_path, broken_api, MORNING, FakeNotifier())
    pages = [_flip(TC_PAGES[0], MEW, True, "18.95"), TC_PAGES[1]]
    flipped = shop_handler(tc_pages=pages)

    def broken_api_flipped(request):
        if request.url.host == "api.pokemontcg.io":
            return httpx.Response(502, text="error code: 502")
        return flipped(request)

    notifier = FakeNotifier()
    assert _main(tmp_path, broken_api_flipped, MORNING, notifier) == 0
    (alert,) = notifier.sent
    assert alert.kind is AlertKind.BUY_NOW
    assert "Chase cards" not in alert.body


def test_set_info_is_cached_in_state(tmp_path):
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    state = load_state(tmp_path / "state.json")
    assert state.tcg_cache["30th Celebration"]["found"] is True


def _barcode_page(inner, log=None, down=False):
    """Zatu product .json pages, for the box scanner's barcodes."""
    def handler(request):
        if request.url.host == "www.zatu.com" and request.url.path.endswith(".json") and "/products/" in request.url.path:
            if log is not None:
                log.append("request barcode")
            if down:
                return httpx.Response(429)
            return httpx.Response(200, text=(FIX / "zatu_product_dr_etb.json").read_text())
        return inner(request)
    return handler


def test_barcodes_are_fetched_after_the_alerts_and_saved(tmp_path):
    _main(tmp_path, _barcode_page(shop_handler()), MORNING, FakeNotifier())
    log = []

    class OrderedNotifier(FakeNotifier):
        def send(self, alert):
            log.append(f"send {alert.kind.value}")
            return super().send(alert)

    _main(tmp_path, _barcode_page(shop_handler(etb_available=True), log), MORNING, OrderedNotifier())
    assert "send BUY_NOW" in log and "request barcode" in log
    assert log.index("send BUY_NOW") < log.index("request barcode")
    state = load_state(tmp_path / "state.json")
    assert any(e["barcode"] == "196214165809" for e in state.barcodes.values())


def test_barcode_page_outage_never_fails_the_run(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, _barcode_page(shop_handler(), down=True), MORNING, notifier)
    assert _main(tmp_path, _barcode_page(shop_handler(etb_available=True), down=True), MORNING, notifier) == 0
    assert [a.kind for a in notifier.sent] == [AlertKind.BUY_NOW]


# --- healthchecks.io heartbeat ---------------------------------------------

HC = "https://hc-ping.com/abc-123"


def _with_hc(inner, pings):
    def handler(request):
        if request.url.host == "hc-ping.com":
            pings.append(str(request.url))
            return httpx.Response(200, text="OK")
        return inner(request)
    return handler


def test_heartbeat_pinged_after_a_good_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_URL", HC)
    pings = []
    assert _main(tmp_path, _with_hc(shop_handler(), pings), MORNING, FakeNotifier()) == 0
    assert pings == [HC]


def test_heartbeat_reports_fail_when_an_alert_is_undelivered(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_URL", HC)
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    pings = []
    rc = _main(tmp_path, _with_hc(shop_handler(etb_available=True), pings), MORNING, DeadNotifier())
    assert rc == 2
    assert pings == [HC + "/fail"]


def test_heartbeat_outage_does_not_fail_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_URL", HC)
    inner = shop_handler()

    def handler(request):
        if request.url.host == "hc-ping.com":
            raise httpx.ConnectError("down")
        return inner(request)

    assert _main(tmp_path, handler, MORNING, FakeNotifier()) == 0


def test_no_heartbeat_without_the_secret_or_on_test_modes(tmp_path, monkeypatch):
    pings = []
    _main(tmp_path, _with_hc(shop_handler(), pings), MORNING, FakeNotifier())
    monkeypatch.setenv("HEALTHCHECK_URL", HC)
    _main(tmp_path, _with_hc(shop_handler(), pings), MORNING, FakeNotifier(), argv=["--test-notify"])
    assert pings == []


def test_workflow_retries_the_state_push_and_uses_current_actions():
    wf = (ROOT / ".github" / "workflows" / "watch.yml").read_text()
    # A clashing save (another commit landed first) is retried, not fatal.
    assert "for attempt in 1 2 3" in wf
    assert "git pull --rebase -X theirs && git push && exit 0" in wf
    # Node 20 based action versions are deprecated by GitHub.
    assert "actions/checkout@v7" in wf and "actions/setup-python@v7" in wf
    assert "@v4" not in wf and "@v5" not in wf


ZATU_MEW = "pok-mon-tcg-30th-celebration-mini-tin-mew"


def test_product_in_two_collections_is_listed_once(tmp_path):
    # The fake Zatu serves the same page for every collection, so each 30th
    # product turns up in all of them — the Pokédex update must list it once.
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), AFTER_NINE, notifier)
    (report,) = [a for a in notifier.sent if "ZATU" in a.body]
    zatu = report.body[report.body.index("🏪 ZATU"):report.body.index("🏪 THE GAME COLLECTION")]
    assert zatu.count("Binder Collection") == 1


def test_manual_modes_queue_separately_from_checks():
    # GitHub keeps one pending run per concurrency group: a Pokédex update
    # queued behind a check was cancelled by the next scheduled check.
    wf = (ROOT / ".github" / "workflows" / "watch.yml").read_text()
    group = next(line for line in wf.split("concurrency:")[1].splitlines() if "group:" in line)
    assert "'watch'" in group and "'watch-manual'" in group


# --- fast lane ------------------------------------------------------------

def _after_main_pass(tmp_path, calm, live):
    """Serve `calm` until this run's main pass has saved state, then `live`."""
    def handler(request):
        state = load_state(tmp_path / "state.json")
        return (live if state and state.total_checks >= 2 else calm)(request)
    return handler


def _round_times(caplog):
    return [float(m.group(1)) for r in caplog.records if (m := re.match(r"fast lane round \d+ at (\d+)s", r.getMessage()))]


def test_fast_lane_catches_stock_between_runs(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    # In stock only once the main pass is over: just the fast lane can see it.
    _main(tmp_path, _after_main_pass(tmp_path, shop_handler(), shop_handler(etb_available=True)), MORNING, notifier)
    assert [a.kind for a in notifier.sent] == [AlertKind.BUY_NOW]
    assert "Delta Reign Elite Trainer Box" in notifier.sent[0].title
    assert load_state(tmp_path / "state.json").products["tc-dr-etb"].status == "AVAILABLE"


def _round_ends(caplog):
    return [float(m.group(1)) for r in caplog.records if (m := re.match(r"fast lane round \d+ done at (\d+)s", r.getMessage()))]


def test_fast_lane_finishes_before_the_next_run(tmp_path, caplog):
    caplog.set_level("INFO")
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    settings, _ = run.load_config(ROOT / "watchlist.yaml")
    rounds, ends = _round_times(caplog), _round_ends(caplog)
    assert rounds and len(ends) == len(rounds)  # at least one extra look per run
    # The round must END in time, not just start in time.
    assert all(t <= settings.fast_lane_stop_after_seconds for t in ends)
    assert all(b - a >= settings.fast_lane_every_seconds for a, b in zip(rounds, rounds[1:]))


def test_fast_lane_fits_a_second_round_after_a_long_main_pass(tmp_path, caplog):
    # Live, 27 Oct: ~150 products make the main pass end at ~148 s and a round take
    # ~31 s, yet only one round ran - the up-front guess (5 s a request) said a
    # second one wouldn't finish by 245 s. Once a round has been timed, trust it.
    caplog.set_level("INFO")
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())  # a state to work on
    settings, products = run.load_config(ROOT / "watchlist.yaml", {"TELEGRAM_CHAT_ID": "1", "NTFY_TOPIC": "t"})
    clock, calm = FakeClock(), shop_handler()
    clock.t = 148.0

    def handler(request):  # a quick shop: ~0.3 s a request on top of the polite gap
        clock.t += 0.3
        return calm(request)

    run._fast_lane(
        products, load_state(tmp_path / "state.json"), settings,
        httpx.Client(transport=httpx.MockTransport(handler)), FakeNotifier(), MORNING,
        0.0, 10.0, clock, clock.sleep,
        lambda url: (lambda r: (r.status_code, r.text))(handler(httpx.Request("GET", url))),
        tmp_path / "state.json",
    )
    rounds, ends = _round_times(caplog), _round_ends(caplog)
    assert len(rounds) >= 2
    assert all(t <= settings.fast_lane_stop_after_seconds for t in ends)


def test_fast_lane_is_off_after_its_end_date(tmp_path, caplog):
    caplog.set_level("INFO")
    later = datetime(2027, 1, 6, 7, 0, tzinfo=timezone.utc)
    _main(tmp_path, shop_handler(), later, FakeNotifier())
    _main(tmp_path, shop_handler(), later, FakeNotifier())
    assert _round_times(caplog) == []


def test_fast_lane_never_counts_errors(tmp_path):
    # A shop blocking the fast lane must not race the error alert threshold.
    _main(tmp_path, shop_handler(), MORNING, FakeNotifier())
    calm = shop_handler()
    blocked = lambda r: httpx.Response(429) if "suggest.json" in str(r.url) else calm(r)
    _main(tmp_path, _after_main_pass(tmp_path, calm, blocked), MORNING, FakeNotifier())
    assert load_state(tmp_path / "state.json").products["tc-dr-etb"].consecutive_errors == 0


def test_test_alarm_without_pushover_keys_fails_loudly(tmp_path):
    assert _main(tmp_path, shop_handler(), MORNING, FakeNotifier(), argv=["--test-alarm"]) == 1


def test_test_alarm_sends_an_alarm(tmp_path, monkeypatch):
    monkeypatch.setenv("PUSHOVER_USER_KEY", "u")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "t")
    notifier = FakeNotifier()
    assert _main(tmp_path, shop_handler(), MORNING, notifier, argv=["--test-alarm"]) == 0
    (alert,) = notifier.sent
    assert alert.alarm is True and "TEST" in alert.title
    # A real alarm product, so the test shows the real basket button too.
    assert alert.extra_buttons and "/cart" in alert.extra_buttons[0][1]
