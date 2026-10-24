import re
from pathlib import Path

import pytest

from pokemon_watcher.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.yaml"
ENV = {"TELEGRAM_CHAT_ID": "1", "NTFY_TOPIC": "t"}


def test_loads_all_products():
    _, products = load_config(WATCHLIST, ENV)
    assert len(products) == 14
    assert len({p.key for p in products}) == 14
    assert all(p.query for p in products if p.shop == "totalcards")


def test_urls_are_built_per_shop():
    _, products = load_config(WATCHLIST, ENV)
    by_key = {p.key: p for p in products}
    assert by_key["tc-dr-etb"].url == (
        "https://totalcards.net/products/pokemon-mega-evolution-delta-reign-elite-trainer-box"
    )
    assert by_key["mm-dr-etb"].url == (
        "https://magicmadhouse.co.uk/pokemon-me-delta-reign-elite-trainer-box-mega-rayquaza"
    )


def test_settings_from_yaml():
    settings, _ = load_config(WATCHLIST, ENV)
    assert settings.over_price_tolerance == 0.25
    assert settings.request_gap_seconds == 4


def test_secrets_come_from_env():
    settings, _ = load_config(WATCHLIST, ENV)
    assert settings.telegram_chat_id == "1"
    assert settings.ntfy_topic == "t"


def test_missing_secret_raises():
    with pytest.raises(ConfigError, match="NTFY_TOPIC"):
        load_config(WATCHLIST, {"TELEGRAM_CHAT_ID": "1"})


def test_unknown_shop_raises(tmp_path):
    bad = tmp_path / "w.yaml"
    bad.write_text("products:\n  - {key: a, label: A, shop: argos, handle: x}\n")
    with pytest.raises(ConfigError, match="argos"):
        load_config(bad, ENV)


def test_yaml_contains_no_secrets():
    text = WATCHLIST.read_text()
    assert not re.search(r"\d{8,}", text)          # Telegram chat ids are long numbers
    assert not re.search(r"\d+:[A-Za-z0-9_-]{30,}", text)  # bot token shape
    assert "ntfy_topic" not in text
