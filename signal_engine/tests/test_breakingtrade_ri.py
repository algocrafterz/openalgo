"""BREAKINGTRADE-RI: the range-integrity version, paper traded beside BREAKINGTRADE (2026-10-28).

Same scanner, same picks, same stop/target/exit rules as BREAKINGTRADE. One change: the
confirming 5m bar must START at or after 10:15 (the IB is complete) and CLOSE beyond the IB
edge in the trade's direction, as well as beyond the signal bar's extreme. BREAKINGTRADE
itself must keep emitting exactly as before.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

import signal_engine.config as _se_config
from signal_engine import strategies, strategy_cards
from signal_engine.analysis.breakingtrade import alerts, entry_watch, store, tp_watch, trigger
from signal_engine.analysis.breakingtrade.__main__ import _emit_trade_signals
from signal_engine.config import TelegramChannel


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr(alerts, "send", lambda text, kind=None, monospace=False: (False, None))
    monkeypatch.setattr(alerts.review, "trading_mode", lambda: ("analyze", True))
    alerts._mode_cache["checked_at"] = 0.0


def _bars(rows, start="09:15"):
    """rows: (high, low, close) per 5m bar from `start`."""
    base = pd.Timestamp(f"2026-10-27 {start}")
    return pd.DataFrame([
        {"timestamp": base + pd.Timedelta(minutes=5 * i), "open": c, "high": h, "low": lo,
         "close": c, "volume": 1000}
        for i, (h, lo, c) in enumerate(rows)
    ])


# IB 09:15-10:15 ranges 95-105. The signal bar is the 09:55 bar (high 103).
IB = [(105, 95, 100)] * 8 + [(103, 99, 102)] + [(104, 100, 103)] * 3


class TestPlanner:
    def test_bar_inside_the_ib_confirms_baseline_but_not_ri(self):
        bars = _bars(IB + [(105, 102, 104)])  # 10:15 bar closes 104: above 103, inside IB
        signal = datetime(2026, 10, 27, 9, 57)
        assert trigger.plan_trade("X", "up", None, bars, signal, 1.0) is not None
        assert trigger.plan_trade_range_integrity("X", "up", None, bars, signal, 1.0) is None

    def test_first_close_beyond_the_ib_after_1015_is_the_ri_entry(self):
        bars = _bars(IB + [(105, 102, 104), (108, 104, 107)])
        plan = trigger.plan_trade_range_integrity(
            "X", "up", None, bars, datetime(2026, 10, 27, 9, 57), 1.0
        )
        assert plan.entry == 107.0
        assert plan.triggered_at == datetime(2026, 10, 27, 10, 20)
        assert plan.range_integrity is True

    def test_close_beyond_the_ib_before_1015_does_not_count(self):
        early = [(105, 95, 100)] * 8 + [(103, 99, 102), (107, 101, 106)]  # 10:00 closes 106
        bars = _bars(early + [(106, 104, 105)] * 2 + [(105, 103, 104)])
        plan = trigger.plan_trade_range_integrity(
            "X", "up", None, bars, datetime(2026, 10, 27, 9, 57), 1.0
        )
        assert plan is None

    def test_levels_match_the_baseline_rules(self):
        bars = _bars(IB + [(108, 104, 107)])
        plan = trigger.plan_trade_range_integrity(
            "X", "up", None, bars, datetime(2026, 10, 27, 9, 57), 2.0
        )
        assert plan.stop == pytest.approx(95 - 0.25 * 2.0)
        assert plan.targets[0] == pytest.approx(107 + 10)

    def test_short_mirrors_below_the_ib_low(self):
        rows = [(105, 95, 100)] * 8 + [(101, 97, 98)] + [(100, 96, 97)] * 3 + [(97, 92, 93)]
        plan = trigger.plan_trade_range_integrity(
            "X", "down", None, _bars(rows), datetime(2026, 10, 27, 9, 57), 1.0
        )
        assert plan.entry == 93.0 and plan.range_integrity is True


def _settings(ri_enabled):
    from types import SimpleNamespace

    return SimpleNamespace(
        telegram_channels=[
            TelegramChannel(name="intraday-breakingtrade-analyze", id=-100),
            TelegramChannel(name="intraday-breakingtrade-ri-analyze", id=-300, enabled=ri_enabled),
        ],
        breakingtrade_btst_channels={},
    )


class TestRouting:
    def test_ri_signals_route_to_the_ri_channel(self, monkeypatch):
        monkeypatch.setattr(_se_config, "settings", _settings(True))
        assert alerts.chat_id_for("trade_signal_ri") == "-300"
        assert alerts.chat_id_for("trade_signal") == "-100"

    def test_ri_enabled_follows_the_channel_flag(self, monkeypatch):
        monkeypatch.setattr(_se_config, "settings", _settings(True))
        assert alerts.ri_enabled() is True
        monkeypatch.setattr(_se_config, "settings", _settings(False))
        assert alerts.ri_enabled() is False


def _run_emit(monkeypatch, ri_enabled, bars):
    monkeypatch.setattr(_se_config, "settings", _settings(ri_enabled))
    monkeypatch.setattr("signal_engine.analysis.breakingtrade.validate.fetch_bars",
                        lambda symbol, at, exchange="NSE": bars)
    snapshot = pd.DataFrame({"symbol": ["SBIN"], "day_type": ["Trend"], "price": [100.0]})
    emitted = _emit_trade_signals({"Breakaway Above PDH": ["SBIN"]}, snapshot,
                                  datetime(2026, 10, 27, 9, 57))
    with alerts._connect() as conn:
        rows = conn.execute("SELECT kind, strategy FROM alerts ORDER BY rowid").fetchall()
    return emitted, rows


class TestEmit:
    def test_enabled_ri_emits_beside_the_baseline(self, monkeypatch):
        emitted, rows = _run_emit(monkeypatch, True, _bars(IB + [(108, 104, 107)]))
        assert emitted == 2
        assert rows == [("trade_signal", "BREAKINGTRADE"), ("trade_signal_ri", "BREAKINGTRADE-RI")]

    def test_disabled_ri_leaves_the_baseline_alone(self, monkeypatch):
        emitted, rows = _run_emit(monkeypatch, False, _bars(IB + [(108, 104, 107)]))
        assert emitted == 1
        assert rows == [("trade_signal", "BREAKINGTRADE")]

    def test_baseline_can_fire_while_ri_waits(self, monkeypatch):
        emitted, rows = _run_emit(monkeypatch, True, _bars(IB + [(105, 102, 104)]))
        assert rows == [("trade_signal", "BREAKINGTRADE")]


def _record_hit(symbol, direction, scan, captured_at):
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scan_hits (captured_at, scan, direction, symbol, is_new) "
            "VALUES (?, ?, ?, ?, 1)",
            (store._iso(captured_at), scan, direction, symbol),
        )


class TestRecheck:
    def test_each_version_confirms_on_its_own_poll(self, monkeypatch):
        monkeypatch.setattr(_se_config, "settings", _settings(True))
        seen = datetime(2026, 10, 27, 9, 57)
        _record_hit("SBIN", "up", "Breakaway Above PDH", seen)
        current = {"bars": _bars(IB + [(105, 102, 104)])}
        monkeypatch.setattr("signal_engine.analysis.breakingtrade.validate.fetch_bars",
                            lambda symbol, at, exchange="NSE": current["bars"])

        assert entry_watch.check(seen + timedelta(minutes=19)) == 1  # baseline only
        current["bars"] = _bars(IB + [(105, 102, 104), (108, 104, 107)])
        assert entry_watch.check(seen + timedelta(minutes=34)) == 1  # RI now, baseline not again
        with alerts._connect() as conn:
            kinds = [r[0] for r in conn.execute("SELECT kind FROM alerts ORDER BY rowid")]
        assert kinds == ["trade_signal", "trade_signal_ri"]


class TestWiring:
    def test_tp_watch_manages_ri_positions_on_their_own_channel(self):
        assert ("BREAKINGTRADE-RI", "trade_signal_ri") in tp_watch._WATCHED_STRATEGIES

    def test_registry_and_card(self):
        assert strategies.channel_base("BREAKINGTRADE-RI") == "intraday-breakingtrade-ri"
        assert "intraday-breakingtrade-ri" in strategy_cards.CARDS
