import json
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


def shop_handler(etb_available=False, js_requests=None):
    """All TC queries return every watched handle; MM returns the fixture page."""
    from pokemon_watcher.config import load_config

    _, products = load_config(ROOT / "watchlist.yaml", {"TELEGRAM_CHAT_ID": "1", "NTFY_TOPIC": "t"})
    tc = [p for p in products if p.shop == "totalcards"]

    def handler(request):
        url = str(request.url)
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
    assert len(state.products) == 14
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


def test_report_sent_once_per_day(tmp_path):
    notifier = FakeNotifier()
    _main(tmp_path, shop_handler(), MORNING, notifier)      # baseline, before 9
    _main(tmp_path, shop_handler(), AFTER_NINE, notifier)   # report
    _main(tmp_path, shop_handler(), AFTER_NINE, notifier)   # not again
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
    monkeypatch.delenv("NTFY_TOPIC")
    assert _main(tmp_path, shop_handler(), MORNING, FakeNotifier()) == 1
