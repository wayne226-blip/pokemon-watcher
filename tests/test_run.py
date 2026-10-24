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


def _main(tmp_path, handler, now, notifier, argv=None):
    return run.main(
        argv or ["--once"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        notifier=notifier,
        now=now,
        sleep=lambda s: None,
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
    assert len(state.products) == 84
    assert "totalcards-pokemon-30th-celebration-mini-tin-mew" in state.products
    assert state.total_checks == 1


def test_second_run_available_sends_buy_now(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    _main(tmp_path, shop_handler(etb_available=True), MORNING, notifier)
    kinds = [a.kind for a in notifier.sent]
    assert kinds == [AlertKind.BUY_NOW]
    assert "Delta Reign Elite Trainer Box" in notifier.sent[0].title


def test_not_seen_fallback_fetches_js_after_12(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)
    state = load_state(tmp_path / "state.json")
    state.products["tc-upc-day"].consecutive_not_seen = 12
    save_state(tmp_path / "state.json", state)

    js = []
    _main(tmp_path, shop_handler(js_requests=js), MORNING, notifier)
    assert js == ["https://totalcards.net/products/pokemon-30th-celebration-ultra-premium-collection-day.js"]


def test_report_sent_once_per_slot(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)      # baseline, before 9
    _main(tmp_path, shop_handler(), AFTER_NINE, notifier)   # 9am report
    _main(tmp_path, shop_handler(), AFTER_NINE, notifier)   # not again
    assert [a.kind for a in notifier.sent] == [AlertKind.REPORT]


def test_four_reports_a_day(tmp_path):
    # 9am, 1pm, 5pm, 9pm London. November = GMT, so London hour == UTC hour.
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)  # baseline 07:00
    for hh, mm in [(9, 30), (9, 35), (12, 0), (13, 5), (13, 10), (17, 0), (21, 10), (21, 15), (23, 50)]:
        at = datetime(2026, 11, 6, hh, mm, tzinfo=timezone.utc)
        _main(tmp_path, shop_handler(), at, notifier)
    assert [a.kind for a in notifier.sent] == [AlertKind.REPORT] * 4


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


def test_zatu_mini_tin_over_cap_stays_quiet(tmp_path):
    notifier = FakeNotifier()
    page = _flip(ZATU_PAGE, "pok-mon-tcg-30th-celebration-mini-tin-mew", False, "29.99")
    _main(tmp_path, shop_handler(zatu_page=page), MORNING, notifier)
    page = _flip(ZATU_PAGE, "pok-mon-tcg-30th-celebration-mini-tin-mew", True, "29.99")
    _main(tmp_path, shop_handler(zatu_page=page), MORNING, notifier)
    assert notifier.sent == []


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
    # Zatu sells several 30th products in stock above the caps: they must count.
    zatu = report.body[report.body.index("🏪 ZATU"):report.body.index("🏪 THE GAME COLLECTION")]
    assert re.search(r"💸 Silly money \([1-9]", zatu)


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
