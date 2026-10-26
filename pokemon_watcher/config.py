"""Load watchlist.yaml. Secrets come from the environment, never the file."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from .models import Product, Settings

SHOP_URLS = {
    "totalcards": "https://totalcards.net/products/{handle}",
    "zatu": "https://www.zatu.com/products/{handle}",
    "tgc": "https://collect.thegamecollection.net/products/{handle}",
    "magicmadhouse": "https://magicmadhouse.co.uk/{handle}",
}

SECRET_ENV = {"telegram_chat_id": "TELEGRAM_CHAT_ID"}
OPTIONAL_ENV = {"ntfy_topic": "NTFY_TOPIC"}  # unset = no ntfy, Telegram only


class ConfigError(Exception):
    pass


def load_config(
    path: Path, env: Mapping[str, str] = os.environ
) -> tuple[Settings, list[Product]]:
    raw = yaml.safe_load(Path(path).read_text()) or {}

    secrets = {}
    for field_name, var in SECRET_ENV.items():
        value = env.get(var)
        if not value:
            raise ConfigError(f"environment variable {var} is not set")
        secrets[field_name] = value
    for field_name, var in OPTIONAL_ENV.items():
        secrets[field_name] = env.get(var) or None

    options = dict(raw.get("settings") or {})
    options["collections"] = tuple(
        (
            c["shop"],
            c["handle"],
            tuple(str(w).lower() for w in c.get("include") or []),
            tuple(str(w).lower() for w in c.get("exclude") or []),
        )
        for c in options.pop("collections", None) or raw.get("collections") or []
    )
    options["price_caps"] = tuple(
        (str(c["match"]).lower(), float(c["max"])) for c in options.pop("price_caps", None) or raw.get("price_caps") or []
    )
    options["tcg_sets"] = tuple(options.pop("tcg_sets", None) or raw.get("tcg_sets") or [])
    fast = raw.get("fast_lane") or {}
    if fast:
        options["fast_lane_keys"] = tuple(fast.get("keys") or [])
        options["fast_lane_until"] = str(fast["until"]) if fast.get("until") else None
        for name in ("every_seconds", "stop_after_seconds"):
            if name in fast:
                options[f"fast_lane_{name}"] = float(fast[name])
    for shop, *_ in options["collections"]:
        if shop not in ("totalcards", "zatu"):
            raise ConfigError(f"collections are only supported on Shopify shops, not {shop!r}")
    if "report_hours" in options:
        options["report_hours"] = tuple(sorted(int(h) for h in options["report_hours"]))
    settings = Settings(**secrets, **options)

    products = []
    for entry in raw.get("products") or []:
        shop = entry.get("shop")
        if shop not in SHOP_URLS:
            raise ConfigError(f"unknown shop {shop!r} for product {entry.get('key')}")
        products.append(
            Product(
                key=entry["key"],
                label=entry["label"],
                shop=shop,
                url=SHOP_URLS[shop].format(handle=entry["handle"]),
                handle=entry["handle"],
                query=entry.get("query"),
                shop_price=entry.get("shop_price"),
                variant_id=entry.get("variant_id"),
            )
        )
    unknown = set(settings.fast_lane_keys) - {p.key for p in products}
    if unknown:
        raise ConfigError(f"fast_lane lists unknown product keys: {sorted(unknown)}")
    return settings, products
