"""Push alerts to Telegram and ntfy.sh.

Both channels fire on every alert. The marginal cost is one extra HTTP POST
and a missed notification is the only real failure mode this project has.

The Telegram bot token must never reach a log line. Note that httpx embeds
the full request URL in its exception strings, and that URL contains the
token — so error paths here log the exception *type*, never its text.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable

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
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
PUSHOVER_RETRY_SECONDS = 30     # the siren repeats this often ...
PUSHOVER_EXPIRE_SECONDS = 1800  # ... until acknowledged, for up to 30 minutes
NTFY_PRIORITY = {"high": "5", "default": "3", "low": "2"}
TIMEOUT = 15.0
MAX_RETRY_AFTER = 30.0   # never stall a run longer than this on one 429
SEND_GAP_SECONDS = 1.0   # Telegram allows about one message a second per chat

_TAG = re.compile(r"<[^>]+>")
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




def _post(client: httpx.Client, url: str, data: dict, sleep: Callable[[float], None]) -> httpx.Response:
    """POST once; on a 429, wait as long as Telegram asks (capped) and retry once."""
    response = client.post(url, data=data, timeout=TIMEOUT)
    if response.status_code != 429:
        return response
    try:
        wait = float(response.json().get("parameters", {}).get("retry_after", 1))
    except (ValueError, AttributeError):
        wait = 1.0
    wait = min(wait, MAX_RETRY_AFTER)
    log.warning("Telegram rate limit — waiting %.0fs and retrying", wait)
    sleep(wait)
    return client.post(url, data=data, timeout=TIMEOUT)


def send_telegram(
    alert: Alert, token: str, chat_id: str, client: httpx.Client, sleep: Callable[[float], None] = time.sleep
) -> bool:
    buttons = []
    if alert.url and alert.button_label:
        buttons.append([{"text": alert.button_label, "url": alert.url}])
    for label, url in alert.extra_buttons:
        buttons.append([{"text": label, "url": url}])

    if alert.html:
        text = f"<b>{html.escape(alert.title, quote=False)}</b>\n\n{alert.body}"
    else:
        text = f"*{alert.title}*\n\n{alert.body}"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML" if alert.html else "Markdown",
        "disable_web_page_preview": True,
    }
    if alert.silent:
        payload["disable_notification"] = "true"  # no sound: Popcorn means "act now"
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})

    if alert.image_url and len(payload["text"]) <= PHOTO_CAPTION_LIMIT:
        photo = {k: v for k, v in payload.items() if k not in ("text", "disable_web_page_preview")}
        photo.update(photo=alert.image_url, caption=payload["text"])
        try:
            response = _post(client, f"{TELEGRAM_API}/bot{token}/sendPhoto", photo, sleep)
            if response.status_code == 200:
                return True
            log.warning("Telegram sendPhoto returned HTTP %s — sending as text", response.status_code)
        except httpx.HTTPError as exc:
            log.warning("Telegram sendPhoto failed (%s) — sending as text", type(exc).__name__)

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        response = _post(client, url, payload, sleep)
        if response.status_code == 400:
            # Formatting rejected (e.g. a stray "_" in a product name): an
            # alert sent plain still beats an alert not sent at all.
            log.warning("Telegram rejected %s — resending as plain text", payload["parse_mode"])
            payload.pop("parse_mode")
            body = html.unescape(_TAG.sub("", alert.body)) if alert.html else alert.body
            payload["text"] = f"{alert.title}\n\n{body}"
            response = _post(client, url, payload, sleep)
    except httpx.HTTPError as exc:
        # Type only — the exception text contains the tokenised URL.
        log.error("Telegram send failed: %s", type(exc).__name__)
        return False

    if response.status_code != 200:
        log.error("Telegram send returned HTTP %s", response.status_code)
        return False
    return True


def send_pushover(alert: Alert, token: str, user: str, client: httpx.Client) -> bool:
    """Emergency priority: on iPhone a Critical Alert (rings on silent and
    through Focus) that repeats until Wayne acknowledges it. The tap goes
    straight to checkout when there is one."""
    checkout = next((url for label, url in alert.extra_buttons if "checkout" in label.lower()), None)
    data = {
        "token": token,
        "user": user,
        "title": alert.title[:250],
        "message": _TAG.sub("", alert.body)[:1024],
        "priority": "2",
        "retry": str(PUSHOVER_RETRY_SECONDS),
        "expire": str(PUSHOVER_EXPIRE_SECONDS),
        "sound": "siren",
    }
    if checkout or alert.url:
        data["url"] = checkout or alert.url
        data["url_title"] = "Straight to checkout" if checkout else (alert.button_label or "Open")
    try:
        response = client.post(PUSHOVER_URL, data=data, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        log.error("Pushover send failed: %s", type(exc).__name__)  # type only: never the token
        return False
    try:
        accepted = response.status_code == 200 and response.json().get("status") == 1
    except ValueError:
        accepted = False
    if not accepted:
        # 4xx means bad input (wrong key, quota): retrying the same request won't help.
        log.error("Pushover rejected the alarm: HTTP %s", response.status_code)
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
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.client = client or httpx.Client()
        self.token = token
        self.sleep = sleep
        self.clock = clock
        self._last_telegram: float | None = None
        if not self.token:
            log.error("no TELEGRAM_BOT_TOKEN set — Telegram alerts are DISABLED")

    def send(self, alert: Alert) -> dict[str, bool]:
        if self.token and self._last_telegram is not None:
            # A drop can fire a dozen alerts at once; space them so Telegram
            # doesn't rate-limit the lot.
            gap = SEND_GAP_SECONDS - (self.clock() - self._last_telegram)
            if gap > 0:
                self.sleep(gap)
        results = {
            "telegram": (
                send_telegram(
                    alert, self.token, self.settings.telegram_chat_id, self.client, self.sleep
                )
                if self.token
                else False
            ),
        }
        if self.token:
            self._last_telegram = self.clock()
        if self.settings.ntfy_topic:
            results["ntfy"] = send_ntfy(alert, self.settings.ntfy_topic, self.client)
        if alert.alarm and self.settings.pushover_user and self.settings.pushover_token:
            results["pushover"] = send_pushover(
                alert, self.settings.pushover_token, self.settings.pushover_user, self.client
            )
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
