"""BREAKINGTRADE-WATCHLIST retirement (2026-10-27): its channel's `enabled` flag gates the planner.

The watchlist outcome was compared against the confirmed outcome on paper from 2026-09-24 and
retired. Disabling its channel in config.yaml must stop the scanner from building and sending its
signal at all (no Telegram message, no alert row), while the confirmed BREAKINGTRADE signal keeps
emitting exactly as before. The implementation stays in place so it can be re-enabled by flipping
the flag back.
"""

from datetime import datetime

import pandas as pd
import pytest

import signal_engine.config as _se_config
from signal_engine.analysis.breakingtrade import alerts, store, trigger, validate
from signal_engine.analysis.breakingtrade.__main__ import _emit_trade_signals
from signal_engine.config import TelegramChannel


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr(alerts, "send", lambda text, kind=None, monospace=False: (False, None))
    monkeypatch.setattr(alerts.review, "trading_mode", lambda: ("analyze", True))
    alerts._mode_cache["checked_at"] = 0.0


def _settings(watchlist_enabled):
    from types import SimpleNamespace

    channels = [TelegramChannel(name="intraday-breakingtrade-analyze", id=-100)]
    if watchlist_enabled is not None:
        channels.append(
            TelegramChannel(
                name="intraday-breakingtrade-watchlist-analyze",
                id=-200,
                enabled=watchlist_enabled,
            )
        )
    return SimpleNamespace(telegram_channels=channels, breakingtrade_btst_channels={})


class TestWatchlistEnabled:
    def test_enabled_channel_is_on(self, monkeypatch):
        monkeypatch.setattr(_se_config, "settings", _settings(True))
        assert alerts.watchlist_enabled() is True

    def test_disabled_channel_is_off(self, monkeypatch):
        monkeypatch.setattr(_se_config, "settings", _settings(False))
        assert alerts.watchlist_enabled() is False

    def test_missing_channel_is_off(self, monkeypatch):
        monkeypatch.setattr(_se_config, "settings", _settings(None))
        assert alerts.watchlist_enabled() is False


def _bars():
    stamps = pd.date_range("2026-10-27 09:15", periods=30, freq="5min")
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
        }
    )


def _run_emit(monkeypatch, watchlist_enabled):
    monkeypatch.setattr(_se_config, "settings", _settings(watchlist_enabled))
    monkeypatch.setattr(validate, "fetch_bars", lambda symbol, at: _bars())
    monkeypatch.setattr(validate, "average_true_range", lambda bars: 1.0)
    monkeypatch.setattr(trigger, "plan_trade", lambda *a, **k: "CONFIRMED_PLAN")
    watchlist_calls = []
    monkeypatch.setattr(
        trigger,
        "plan_trade_watchlist",
        lambda *a, **k: watchlist_calls.append(a) or "WATCHLIST_PLAN",
    )
    sent = []
    monkeypatch.setattr(
        alerts,
        "alert_trade_signal",
        lambda plan, strategy="BREAKINGTRADE", scan_name=None, kind="trade_signal": sent.append(
            (plan, strategy, kind)
        ),
    )
    snapshot = pd.DataFrame({"symbol": ["SBIN"], "day_type": ["Trend"], "price": [100.0]})
    emitted = _emit_trade_signals(
        {"The Breakdown": ["SBIN"]}, snapshot, datetime(2026, 10, 27, 11, 0)
    )
    return emitted, sent, watchlist_calls


def test_disabled_watchlist_sends_only_the_confirmed_signal(monkeypatch):
    emitted, sent, watchlist_calls = _run_emit(monkeypatch, watchlist_enabled=False)
    assert sent == [("CONFIRMED_PLAN", "BREAKINGTRADE", "trade_signal")]
    assert watchlist_calls == []
    assert emitted == 1


def test_enabled_watchlist_still_sends_both(monkeypatch):
    emitted, sent, watchlist_calls = _run_emit(monkeypatch, watchlist_enabled=True)
    assert [s[2] for s in sent] == ["trade_signal", "trade_signal_watchlist"]
    assert len(watchlist_calls) == 1
    assert emitted == 2
