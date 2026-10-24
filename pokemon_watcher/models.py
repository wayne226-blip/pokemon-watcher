"""Shared data types. Pure data — no I/O, no framework imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    AVAILABLE = "AVAILABLE"      # can be bought or pre-ordered now
    PRICED = "PRICED"            # has a price but can't be bought yet
    UNAVAILABLE = "UNAVAILABLE"  # listed, not buyable, no price
    NOT_SEEN = "NOT_SEEN"        # missing from this run's search results
    UNKNOWN = "UNKNOWN"          # response no longer looks like we expect
    ERROR = "ERROR"              # network failure or non-200


class AlertKind(str, Enum):
    BUY_NOW = "BUY_NOW"
    PRICED = "PRICED"
    UNCONFIRMED = "UNCONFIRMED"
    SOLD_OUT_AGAIN = "SOLD_OUT_AGAIN"
    SCRAPER_ERROR = "SCRAPER_ERROR"
    REPORT = "REPORT"


SHOP_LABELS = {"totalcards": "Total Cards", "magicmadhouse": "Magic Madhouse"}


@dataclass(frozen=True)
class Product:
    key: str
    label: str
    shop: str        # "totalcards" | "magicmadhouse"
    url: str         # page Wayne taps through to
    handle: str      # Shopify handle (TC) or URL slug (MM)
    query: str | None = None          # TC search term that returns this handle
    shop_price: float | None = None   # normal price; None = not known yet

    @property
    def shop_label(self) -> str:
        return SHOP_LABELS.get(self.shop, self.shop)


@dataclass(frozen=True)
class CheckResult:
    key: str
    status: Status
    price: float | None = None
    error: str | None = None
    checked_at: str = ""


@dataclass(frozen=True)
class Settings:
    telegram_chat_id: str
    ntfy_topic: str
    cooldown_minutes: int = 30
    error_threshold: int = 5
    over_price_tolerance: float = 0.25
    not_seen_fallback_runs: int = 12
    sold_out_again_hours: int = 24
    report_hour: int = 9
    request_gap_seconds: float = 4.0


@dataclass
class Alert:
    kind: AlertKind
    title: str
    body: str
    url: str | None = None
    button_label: str | None = None
    priority: str = "default"
    extra_buttons: list[tuple[str, str]] = field(default_factory=list)
