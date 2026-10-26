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


SHOP_LABELS = {
    "totalcards": "Total Cards",
    "zatu": "Zatu",
    "tgc": "The Game Collection",
    "magicmadhouse": "Magic Madhouse",
}


@dataclass(frozen=True)
class Product:
    key: str
    label: str
    shop: str        # "totalcards" | "zatu" | "magicmadhouse"
    url: str         # page Wayne taps through to
    handle: str      # Shopify handle (TC) or URL slug (MM)
    query: str | None = None          # Shopify search term that returns this handle
    shop_price: float | None = None   # normal price; None = not known yet
    max_price: float | None = None    # hard cap for collection products
    collection: str | None = None     # set when discovered from a shop collection
    variant_id: int | None = None     # Shopify variant, for a straight-to-checkout link

    @property
    def shop_label(self) -> str:
        return SHOP_LABELS.get(self.shop, self.shop)

    @property
    def checkout_url(self) -> str | None:
        # Shopify cart permalink: opens checkout with one of this already in the basket.
        if self.variant_id is None or "/products/" not in self.url:
            return None
        return f"{self.url.split('/products/')[0]}/cart/{self.variant_id}:1"


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
    ntfy_topic: str | None = None  # unset = Telegram only
    cooldown_minutes: int = 30
    # BUY NOW only: long enough to stop a 1,0,1,0 flicker spamming, short enough
    # that a restock wave after a sell-out gets its own alert.
    buy_now_cooldown_minutes: int = 5
    error_threshold: int = 5
    over_price_tolerance: float = 0.25
    not_seen_fallback_runs: int = 12
    sold_out_again_hours: int = 24
    report_hours: tuple[int, ...] = (9, 13, 17, 21)  # London time
    request_gap_seconds: float = 4.0
    # (shop, collection handle, include words, exclude words) — empty = no filter
    collections: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = ()
    price_caps: tuple[tuple[str, float], ...] = ()         # (title keyword, max £)
    tcg_sets: tuple[str, ...] = ()  # set names for BUY NOW set info / chase cards
    # Must-have products re-checked about every minute between 5-minute runs,
    # up to and including fast_lane_until (London date, "YYYY-MM-DD").
    fast_lane_keys: tuple[str, ...] = ()
    fast_lane_until: str | None = None
    fast_lane_every_seconds: float = 60.0
    fast_lane_stop_after_seconds: float = 220.0  # leave the run before the next trigger


@dataclass
class Alert:
    kind: AlertKind
    title: str
    body: str
    url: str | None = None
    button_label: str | None = None
    priority: str = "default"
    extra_buttons: list[tuple[str, str]] = field(default_factory=list)
    image_url: str | None = None  # sent as a Telegram photo when set
    html: bool = False  # body is Telegram HTML (already escaped), not Markdown
