"""Outgoing Telegram notification settings are gone; an old config.yaml still loads.

Retired in November 2026: telegram.notify_channel, telegram.notify_level and
telegram.breakingtrade_btst_channels. Inbound channels, ingest sources and the Telethon
credentials are untouched.
"""

import copy
import dataclasses

import pytest
import yaml

from signal_engine import config

LEGACY_KEYS = ("notify_channel", "notify_level", "breakingtrade_btst_channels")
RETIRED_ATTRS = LEGACY_KEYS + ("telegram_bot_token", "telegram_notify_chat_id")


def _repo_yaml() -> dict:
    with open(config._YAML_PATH) as f:
        return yaml.safe_load(f)


def _legacy_yaml() -> dict:
    data = copy.deepcopy(_repo_yaml())
    data["telegram"].update(
        {
            "notify_channel": {
                "analyze": {"name": "signal-engine-analyze", "id": -1001},
                "live": {"name": "signal-engine-live", "id": -1002},
            },
            "notify_level": "alerts",
            "breakingtrade_btst_channels": {
                "analyze": {"name": "btst-analyze", "id": -1003},
                "live": {"name": "btst-live", "id": -1004},
            },
        }
    )
    return data


@pytest.fixture
def legacy_settings(monkeypatch):
    monkeypatch.setattr(config, "_load_yaml", _legacy_yaml)
    return config._build_settings()


def test_legacy_keys_load_without_error_and_are_ignored(legacy_settings):
    for name in RETIRED_ATTRS:
        assert not hasattr(legacy_settings, name), name


def test_channels_match_the_repo_config(legacy_settings):
    raw = _repo_yaml()["telegram"]["channels"]
    expected = [(c["name"], c["id"], c.get("enabled", True)) for c in raw]
    loaded = [(c.name, c.id, c.enabled) for c in legacy_settings.telegram_channels]
    assert len(loaded) == len(expected) > 0
    assert [(n, e) for n, _i, e in loaded] == [(n, e) for n, _i, e in expected]
    assert [str(i) for _n, i, _e in loaded] == [str(i) for _n, i, _e in expected]
    live = {c.name: c.enabled for c in legacy_settings.telegram_channels}
    assert live == {n: e for n, _i, e in expected}


def test_ingest_sources_and_credentials_are_kept(legacy_settings):
    n_ingest = len(_repo_yaml()["telegram"].get("ingest_sources") or [])
    assert len(legacy_settings.ingest_sources) == n_ingest
    for name in ("telegram_api_id", "telegram_api_hash", "telegram_phone"):
        assert hasattr(legacy_settings, name)


def test_settings_expose_no_outgoing_notification_attribute():
    fields = {f.name for f in dataclasses.fields(config.Settings)}
    for name in RETIRED_ATTRS:
        assert name not in fields
    assert not any("notify" in n for n in fields)
    assert not hasattr(config.settings, "notify_channel")
