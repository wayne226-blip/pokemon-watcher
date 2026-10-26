import logging

import httpx

from pokemon_watcher.models import Alert, AlertKind, Settings
from pokemon_watcher.notify import Notifier, ascii_header, send_ntfy, send_telegram

ALERT = Alert(
    kind=AlertKind.BUY_NOW,
    title="BUY NOW — Delta Reign Elite Trainer Box",
    body="Total Cards has Delta Reign Elite Trainer Box available at £49.99. Go.",
    url="https://totalcards.net/products/etb",
    button_label="Buy at Total Cards",
    priority="high",
)
SETTINGS = Settings(telegram_chat_id="999", ntfy_topic="topic")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_telegram_posts_inline_keyboard():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    assert send_telegram(ALERT, "TOK", "999", _client(handler)) is True
    assert "sendMessage" in captured["url"]
    assert "inline_keyboard" in captured["body"]
    assert "Buy" in captured["body"]


def test_send_telegram_reports_failure():
    assert send_telegram(ALERT, "TOK", "999", _client(lambda r: httpx.Response(401))) is False


def test_send_ntfy_sets_action_and_priority_headers():
    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200)

    assert send_ntfy(ALERT, "my-topic", _client(handler)) is True
    assert "view, Buy at Total Cards, https://totalcards.net/products/etb" in captured["headers"]["actions"]
    assert captured["headers"]["priority"] == "5"


def test_low_priority_maps_to_2():
    captured = {}

    def handler(request):
        captured["priority"] = request.headers["priority"]
        return httpx.Response(200)

    low = Alert(kind=AlertKind.SOLD_OUT_AGAIN, title="t", body="b", priority="low")
    send_ntfy(low, "topic", _client(handler))
    assert captured["priority"] == "2"


def test_default_priority_maps_to_3():
    captured = {}

    def handler(request):
        captured["priority"] = request.headers["priority"]
        return httpx.Response(200)

    send_ntfy(Alert(kind=AlertKind.PRICED, title="t", body="b"), "topic", _client(handler))
    assert captured["priority"] == "3"


def test_token_never_appears_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    send_telegram(ALERT, "SUPERSECRETTOKEN", "999", _client(lambda r: httpx.Response(500)))
    assert "SUPERSECRETTOKEN" not in caplog.text


def test_token_never_appears_in_logs_on_transport_error(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        raise httpx.ConnectError("failed connecting to " + str(request.url))

    send_telegram(ALERT, "SUPERSECRETTOKEN", "999", _client(handler))
    assert "SUPERSECRETTOKEN" not in caplog.text


def test_notifier_reports_per_channel_results():
    def handler(request):
        if "telegram" in str(request.url):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500)

    notifier = Notifier(SETTINGS, token="tok", client=_client(handler))
    assert notifier.send(ALERT) == {"telegram": True, "ntfy": False}


def test_notifier_without_token_still_sends_ntfy(caplog):
    notifier = Notifier(SETTINGS, token=None, client=_client(lambda r: httpx.Response(200)))
    assert notifier.send(ALERT) == {"telegram": False, "ntfy": True}
    assert "Telegram alerts are DISABLED" in caplog.text


def test_notifier_logs_critical_when_all_channels_fail(caplog):
    caplog.set_level(logging.DEBUG)
    notifier = Notifier(SETTINGS, token="tok", client=_client(lambda r: httpx.Response(500)))
    notifier.send(ALERT)
    assert "ALERT NOT DELIVERED" in caplog.text


def test_ascii_header_folds_em_dash_and_pound():
    assert ascii_header("BUY NOW — ETB") == "BUY NOW - ETB"
    assert ascii_header("at £49.99") == "at GBP 49.99"
    assert ascii_header("Pokémon") == "Pokemon"


def test_ntfy_send_survives_non_ascii_title():
    captured = {}

    def handler(request):
        captured["title"] = request.headers["title"]
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(200)

    assert send_ntfy(ALERT, "topic", _client(handler)) is True
    assert captured["title"] == "BUY NOW - Delta Reign Elite Trainer Box"
    assert "£49.99" in captured["body"]


def test_no_ntfy_topic_means_telegram_only():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, json={"ok": True})

    settings = Settings(telegram_chat_id="999", ntfy_topic=None)
    notifier = Notifier(settings, token="tok", client=_client(handler))
    assert notifier.send(ALERT) == {"telegram": True}
    assert hosts == ["api.telegram.org"]


def test_telegram_resends_plain_if_markdown_is_rejected():
    # A product name with an odd "_" or "*" makes Telegram reject Markdown
    # (HTTP 400). The alert must still arrive, as plain text.
    bodies = []

    def handler(request):
        body = request.content.decode()
        bodies.append(body)
        if "parse_mode=Markdown" in body:
            return httpx.Response(400, json={"ok": False, "description": "can't parse entities"})
        return httpx.Response(200, json={"ok": True})

    assert send_telegram(ALERT, "TOK", "999", _client(handler)) is True
    assert len(bodies) == 2
    assert "parse_mode" not in bodies[1]


PHOTO_ALERT = Alert(
    kind=AlertKind.BUY_NOW,
    title="⚡ A wild ETB appeared!",
    body="🔴 IN STOCK",
    url="https://totalcards.net/products/etb",
    button_label="🛒 Catch it at Total Cards",
    priority="high",
    image_url="https://images.scrydex.com/pokemon/me55-160/large",
)


def test_alert_with_image_is_sent_as_photo():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    assert send_telegram(PHOTO_ALERT, "TOK", "999", _client(handler)) is True
    path, body = seen[0]
    assert path.endswith("/sendPhoto")
    assert "caption=" in body and "photo=" in body and "inline_keyboard" in body


def test_photo_failure_falls_back_to_text_message():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/sendPhoto"):
            return httpx.Response(400, json={"ok": False, "description": "wrong file identifier"})
        return httpx.Response(200, json={"ok": True})

    assert send_telegram(PHOTO_ALERT, "TOK", "999", _client(handler)) is True
    assert seen[0].endswith("/sendPhoto") and seen[-1].endswith("/sendMessage")


HTML_ALERT = Alert(
    kind=AlertKind.REPORT,
    title="📟 Pokédex update",
    body="<b>🏪 TOTAL CARDS</b>\n<blockquote expandable>• Tin &amp; Stuff £18.95</blockquote>",
    html=True,
)


def test_html_alert_uses_html_mode_and_bold_title():
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    assert send_telegram(HTML_ALERT, "TOK", "999", _client(handler)) is True
    from urllib.parse import parse_qs
    fields = parse_qs(sent[0])
    assert fields["parse_mode"] == ["HTML"]
    assert fields["text"][0].startswith("<b>📟 Pokédex update</b>\n\n<b>🏪 TOTAL CARDS</b>")


def test_html_title_is_escaped():
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    alert = Alert(kind=AlertKind.REPORT, title="Tins & <Boxes>", body="x", html=True)
    send_telegram(alert, "TOK", "999", _client(handler))
    from urllib.parse import parse_qs
    assert parse_qs(sent[0])["text"][0].startswith("<b>Tins &amp; &lt;Boxes&gt;</b>")


def test_rejected_html_resends_as_clean_plain_text():
    sent = []

    def handler(request):
        sent.append(request.content.decode())
        if "parse_mode" in sent[-1]:
            return httpx.Response(400, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    assert send_telegram(HTML_ALERT, "TOK", "999", _client(handler)) is True
    from urllib.parse import parse_qs
    plain = parse_qs(sent[1])["text"][0]
    assert "<" not in plain and "&amp;" not in plain
    assert "Tin & Stuff £18.95" in plain


# --- Telegram rate limits -------------------------------------------------

def _rate_limited_then_ok(retry_after, calls):
    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": retry_after}})
        return httpx.Response(200, json={"ok": True})
    return handler


def test_telegram_429_waits_as_told_and_retries():
    calls, waits = [], []
    ok = send_telegram(ALERT, "TOK", "999", _client(_rate_limited_then_ok(3, calls)), sleep=waits.append)
    assert ok is True
    assert waits == [3]
    assert len(calls) == 2


def test_telegram_429_wait_is_capped():
    waits = []
    send_telegram(ALERT, "TOK", "999", _client(_rate_limited_then_ok(999, [])), sleep=waits.append)
    assert waits == [30]


def test_notifier_spaces_out_a_burst():
    waits, now = [], [100.0]
    notifier = Notifier(
        Settings(telegram_chat_id="999"), token="TOK",
        client=_client(lambda r: httpx.Response(200, json={"ok": True})),
        sleep=waits.append, clock=lambda: now[0],
    )
    notifier.send(ALERT)
    notifier.send(ALERT)  # same instant: must wait about a second
    assert waits and 0.9 <= waits[0] <= 1.1


# --- silent messages ------------------------------------------------------

def _captured_body(alert):
    captured = {}

    def handler(request):
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    send_telegram(alert, "TOK", "999", _client(handler))
    return captured["body"]


def test_silent_alert_sends_without_sound():
    quiet = Alert(kind=AlertKind.REPORT, title="Pokédex update", body="all quiet", silent=True)
    assert "disable_notification=true" in _captured_body(quiet)


def test_buy_now_still_rings():
    assert "disable_notification" not in _captured_body(ALERT)


# --- Pushover alarm -------------------------------------------------------

from pokemon_watcher.notify import send_pushover  # noqa: E402

ALARM = Alert(
    kind=AlertKind.BUY_NOW,
    title="⚡ A wild 30th Celebration Elite Trainer Box appeared!",
    body="🔴 IN STOCK at The Game Collection for £49.95!",
    url="https://collect.thegamecollection.net/products/etb",
    button_label="🛒 Catch it at The Game Collection",
    extra_buttons=[("⚡ Straight to checkout", "https://collect.thegamecollection.net/cart/1:1")],
    alarm=True,
)


def _pushover_form(alert):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(200, json={"status": 1, "receipt": "r"})

    ok = send_pushover(alert, "APPTOK", "USERKEY", _client(handler))
    return ok, captured


def test_pushover_alarm_is_an_emergency_that_repeats_until_seen():
    ok, captured = _pushover_form(ALARM)
    form = captured["form"]
    assert ok is True
    assert captured["url"] == "https://api.pushover.net/1/messages.json"
    assert form["priority"] == "2"            # emergency: Critical Alert on iPhone
    assert form["retry"] == "30"              # repeats every 30 s ...
    assert int(form["expire"]) >= 600         # ... until acknowledged or expired
    assert form["sound"] == "siren"
    assert form["url"] == "https://collect.thegamecollection.net/cart/1:1"  # straight to checkout
    assert form["token"] == "APPTOK" and form["user"] == "USERKEY"


def test_notifier_sounds_the_alarm_only_for_alarm_alerts():
    hits = []

    def handler(request):
        hits.append(request.url.host)
        return httpx.Response(200, json={"ok": True, "status": 1})

    settings = Settings(telegram_chat_id="999", pushover_user="U", pushover_token="T")
    notifier = Notifier(settings, token="TOK", client=_client(handler), sleep=lambda s: None)
    assert notifier.send(ALERT) == {"telegram": True}
    hits.clear()
    assert notifier.send(ALARM) == {"telegram": True, "pushover": True}
    assert "api.pushover.net" in hits


def test_no_pushover_keys_means_no_alarm():
    notifier = Notifier(Settings(telegram_chat_id="999"), token="TOK",
                        client=_client(lambda r: httpx.Response(200, json={"ok": True})), sleep=lambda s: None)
    assert notifier.send(ALARM) == {"telegram": True}


def test_pushover_status_not_1_is_a_failure():
    # Pushover's contract: success is HTTP 200 AND "status": 1 in the body.
    client = _client(lambda r: httpx.Response(200, json={"status": 0, "errors": ["user identifier is invalid"]}))
    assert send_pushover(ALARM, "APPTOK", "USERKEY", client) is False
