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

    settings = Settings(**secrets, **(raw.get("settings") or {}))

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
            )
        )
    return settings, products
