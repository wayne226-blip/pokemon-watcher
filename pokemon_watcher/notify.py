"""Push alerts to Telegram and ntfy.sh.

Both channels fire on every alert. The marginal cost is one extra HTTP POST
and a missed notification is the only real failure mode this project has.

The Telegram bot token must never reach a log line. Note that httpx embeds
the full request URL in its exception strings, and that URL contains the
token — so error paths here log the exception *type*, never its text.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata

import httpx

from .models import Alert, Settings

log = logging.getLogger(__name__)

# httpx logs every request at INFO, including the full URL — and the Telegram
# URL contains the bot token. Silencing it at import time means no entry point
# can accidentally write the token to disk.
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_API = "https://api.telegram.org"
PHOTO_CAPTION_LIMIT = 1024  # Telegram caps photo captions; longer goes as text
NTFY_URL = "https://ntfy.sh"
NTFY_PRIORITY = {"high": "5", "default": "3", "low": "2"}
TIMEOUT = 15.0

_TOKEN_IN_URL = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+")

_ASCII_SUBSTITUTIONS = {
    "—": "-",
    "–": "-",
    "£": "GBP ",
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "…": "...",
}


class TokenRedactingFilter(logging.Filter):
    """Belt and braces: scrub anything token-shaped from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — never break logging
            return True
        if _TOKEN_IN_URL.search(message):
            record.msg = _TOKEN_IN_URL.sub("/bot<redacted>", message)
            record.args = ()
        return True


def ascii_header(value: str) -> str:
    """Fold a string to ASCII for use in an HTTP header.

    HTTP header values must be latin-1 encodable. Alert titles contain em
    dashes and pound signs, which raise UnicodeEncodeError inside httpx and
    would have killed every real restock push. The body is sent as a UTF-8
    encoded payload, so the full text survives there.
    """
    for char, replacement in _ASCII_SUBSTITUTIONS.items():
        value = value.replace(char, replacement)
    value = unicodedata.normalize("NFKD", value)
    return value.encode("ascii", "ignore").decode("ascii")




def send_telegram(alert: Alert, token: str, chat_id: str, client: httpx.Client) -> bool:
    buttons = []
    if alert.url and alert.button_label:
        buttons.append([{"text": alert.button_label, "url": alert.url}])
    for label, url in alert.extra_buttons:
        buttons.append([{"text": label, "url": url}])

    payload = {
        "chat_id": chat_id,
        "text": f"*{alert.title}*\n\n{alert.body}",
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})

    if alert.image_url and len(payload["text"]) <= PHOTO_CAPTION_LIMIT:
        photo = {k: v for k, v in payload.items() if k not in ("text", "disable_web_page_preview")}
        photo.update(photo=alert.image_url, caption=payload["text"])
        try:
            response = client.post(f"{TELEGRAM_API}/bot{token}/sendPhoto", data=photo, timeout=TIMEOUT)
            if response.status_code == 200:
                return True
            log.warning("Telegram sendPhoto returned HTTP %s — sending as text", response.status_code)
        except httpx.HTTPError as exc:
            log.warning("Telegram sendPhoto failed (%s) — sending as text", type(exc).__name__)

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        response = client.post(url, data=payload, timeout=TIMEOUT)
        if response.status_code == 400:
            # Markdown rejected (e.g. a stray "_" in a product name): an alert
            # sent plain still beats an alert not sent at all.
            log.warning("Telegram rejected Markdown — resending as plain text")
            payload.pop("parse_mode")
            payload["text"] = f"{alert.title}\n\n{alert.body}"
            response = client.post(url, data=payload, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        # Type only — the exception text contains the tokenised URL.
        log.error("Telegram send failed: %s", type(exc).__name__)
        return False

    if response.status_code != 200:
        log.error("Telegram send returned HTTP %s", response.status_code)
        return False
    return True


def send_ntfy(alert: Alert, topic: str, client: httpx.Client) -> bool:
    headers = {
        "Title": ascii_header(alert.title),
        "Priority": NTFY_PRIORITY.get(alert.priority, "3"),
        "Tags": "card_index",
    }
    actions = []
    if alert.url and alert.button_label:
        actions.append(f"view, {ascii_header(alert.button_label)}, {alert.url}")
    for label, url in alert.extra_buttons:
        actions.append(f"view, {ascii_header(label)}, {url}")
    if actions:
        headers["Actions"] = "; ".join(actions)

    try:
        response = client.post(
            f"{NTFY_URL}/{topic}",
            content=alert.body.encode("utf-8"),
            headers=headers,
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.error("ntfy send failed: %s", type(exc).__name__)
        return False

    if response.status_code >= 300:
        log.error("ntfy send returned HTTP %s", response.status_code)
        return False
    return True


class Notifier:
    def __init__(
        self,
        settings: Settings,
        token: str | None,
        client: httpx.Client | None = None,
    ):
        self.settings = settings
        self.client = client or httpx.Client()
        self.token = token
        if not self.token:
            log.error("no TELEGRAM_BOT_TOKEN set — Telegram alerts are DISABLED")

    def send(self, alert: Alert) -> dict[str, bool]:
        results = {
            "telegram": (
                send_telegram(
                    alert, self.token, self.settings.telegram_chat_id, self.client
                )
                if self.token
                else False
            ),
        }
        if self.settings.ntfy_topic:
            results["ntfy"] = send_ntfy(alert, self.settings.ntfy_topic, self.client)
        if not any(results.values()):
            log.critical(
                "ALERT NOT DELIVERED on any channel: %s — %s",
                alert.title,
                alert.body,
            )
        elif not all(results.values()):
            failed = [k for k, ok in results.items() if not ok]
            log.warning("alert delivered but %s failed", ", ".join(failed))
        return results
