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
