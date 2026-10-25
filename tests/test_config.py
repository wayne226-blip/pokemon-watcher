import re
from pathlib import Path

import pytest

from pokemon_watcher.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.yaml"
ENV = {"TELEGRAM_CHAT_ID": "1", "NTFY_TOPIC": "t"}


def test_loads_all_products():
    _, products = load_config(WATCHLIST, ENV)
    assert len(products) == 51
    assert len({p.key for p in products}) == 51
    assert all(p.query for p in products if p.shop in ("totalcards", "zatu", "tgc"))


def test_urls_are_built_per_shop():
    _, products = load_config(WATCHLIST, ENV)
    by_key = {p.key: p for p in products}
    assert by_key["tc-dr-etb"].url == (
        "https://totalcards.net/products/pokemon-mega-evolution-delta-reign-elite-trainer-box"
    )
    assert by_key["zatu-30th-etb"].url == (
        "https://www.zatu.com/products/pok-mon-tcg-30th-celebration-elite-trainer-box"
    )
    assert by_key["tc-30th-etb"].shop_price == 52.45
    assert by_key["mm-30th-etb"].url == "https://magicmadhouse.co.uk/pokemon-30th-celebration-elite-trainer-box"
    assert by_key["mm-30th-etb"].shop_price == 49.99
    assert by_key["zatu-dr-etb"].url == "https://www.zatu.com/products/pokemon-tcg-mega-evolution-delta-reign-elite-trainer-box"
    assert by_key["zatu-dr-etb"].shop_price == 49.99
    assert by_key["tgc-30th-etb"].url == (
        "https://collect.thegamecollection.net/products/pokemon-tcg-elite-trainer-box-30th-celebration"
    )
    assert by_key["tgc-30th-etb"].shop_label == "The Game Collection"
    assert len([p for p in products if p.key.startswith("tgc-30th-tin-")]) == 9
    assert by_key["zatu-dr-box"].shop_price == 154.44
    assert by_key["zatu-dr-bundle"].query == by_key["zatu-dr-box"].query == "delta reign booster bundle"
    assert by_key["mm-dr-etb"].url == (
        "https://magicmadhouse.co.uk/pokemon-me-delta-reign-elite-trainer-box-mega-rayquaza"
    )


def test_settings_from_yaml():
    settings, _ = load_config(WATCHLIST, ENV)
    assert settings.over_price_tolerance == 0.25
    assert settings.request_gap_seconds == 4
    assert settings.report_hours == (9, 13, 17, 21)
    handles = {(shop, handle) for shop, handle, _, _ in settings.collections}
    assert ("totalcards", "pokemon-30th-celebration") in handles
    assert ("zatu", "pokemon-tcg-30th-celebration") in handles
    assert ("mini tin", 22.0) in settings.price_caps
    assert "Delta Reign" in settings.tcg_sets and "30th Celebration" in settings.tcg_sets


def test_secrets_come_from_env():
    settings, _ = load_config(WATCHLIST, ENV)
    assert settings.telegram_chat_id == "1"
    assert settings.ntfy_topic == "t"


def test_ntfy_is_optional():
    settings, _ = load_config(WATCHLIST, {"TELEGRAM_CHAT_ID": "1"})
    assert settings.ntfy_topic is None


def test_missing_chat_id_raises():
    with pytest.raises(ConfigError, match="TELEGRAM_CHAT_ID"):
        load_config(WATCHLIST, {"NTFY_TOPIC": "t"})


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


def test_collection_include_and_exclude_words(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text(
        "collections:\n"
        "  - {shop: zatu, handle: a, include: [Delta Reign, 30th], exclude: [Sticker]}\n"
        "  - {shop: zatu, handle: b}\n"
    )
    settings, _ = load_config(path, ENV)
    assert settings.collections == (
        ("zatu", "a", ("delta reign", "30th"), ("sticker",)),
        ("zatu", "b", (), ()),
    )
