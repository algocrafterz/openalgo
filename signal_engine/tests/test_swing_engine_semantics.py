"""Swing engine: exit at the open, entries without a stop, and an unlimited holding cap."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import data
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx, EntrySignal
from signal_engine.tests.test_backtest_ledgers import SwingScript


def frame(opens, closes=None, symbol_days=None):
    n = len(opens)
    opens = np.asarray(opens, dtype=float)
    closes = opens.copy() if closes is None else np.asarray(closes, dtype=float)
    idx = pd.bdate_range("2026-01-05", periods=n, tz="Asia/Kolkata")
    raw = pd.DataFrame(
        {
            "Open": opens,
            "High": np.maximum(opens, closes) * 1.001,
            "Low": np.minimum(opens, closes) * 0.999,
            "Close": closes,
            "Volume": 1000.0,
        },
        index=idx,
    )
    out = data._downcast(data._split_adjust_daily(raw))
    out["day"] = pd.Series(out.index.date, index=out.index)
    return out


class Hooked(SwingScript):
    """SwingScript plus the new exit-at-open hook, scripted by bar index."""

    def __init__(self, entries, exits=None, stop=None):
        super().__init__({})
        self.entries, self.exits, self.stop = entries, exits or {}, stop
        self.exit_calls = []

    def entry_at_open(self, c, i, p, direction):
        if direction != 1 or i not in self.entries:
            return None
        return EntrySignal(1, self.stop, math.inf, "T")

    def exit_at_open(self, c, i, p, pos):
        self.exit_calls.append(i)
        return self.exits.get(i)


def run_engine(strategy, df, **cfg):
    return simulate_swing(Ctx(df, "A"), strategy, None, SwingRunConfig(**cfg))


class TestExitAtOpen:
    def test_the_exit_fills_at_that_bars_open_with_the_strategys_reason(self):
        opens = [100, 101, 102, 103, 104, 110, 111, 112]
        s = Hooked({2}, {5: "REBALANCE"}, stop=90.0)
        (t,) = run_engine(s, frame(opens))
        assert t.reason == "REBALANCE"
        assert t.exit == pytest.approx(110.0)
        assert t.entry == pytest.approx(102.0)
        assert pd.Timestamp(t.exit_time) == frame(opens).index[5]

    def test_it_is_not_asked_on_the_entry_bar(self):
        s = Hooked({2}, {2: "TOO_EARLY"}, stop=90.0)
        run_engine(s, frame([100] * 6))
        assert 2 not in s.exit_calls

    def test_it_is_asked_every_later_bar_while_the_position_is_open(self):
        s = Hooked({1}, {}, stop=50.0)
        run_engine(s, frame([100] * 6))
        assert s.exit_calls == [2, 3, 4, 5]

    def test_a_strategy_without_the_hook_is_unaffected(self):
        plain = SwingScript({2: (1, 90.0, math.inf)})
        (t,) = run_engine(plain, frame([100] * 8))
        assert t.reason == "EOD_DATA"

    def test_the_stop_still_applies_when_the_hook_does_not_exit(self):
        opens = [100, 100, 100, 100, 100, 100]
        closes = [100, 100, 100, 80, 80, 80]
        s = Hooked({1}, {}, stop=95.0)
        (t,) = run_engine(s, frame(opens, closes))
        assert t.reason in ("SL", "SL_GAP")

    def test_the_same_symbol_is_not_bought_again_on_the_bar_it_was_sold(self):
        s = Hooked({1, 4}, {4: "REBALANCE"}, stop=50.0)
        trades = run_engine(s, frame([100] * 8))
        assert [t.reason for t in trades][0] == "REBALANCE"
        entry_days = [pd.Timestamp(t.entry_time) for t in trades]
        assert frame([100] * 8).index[4] not in entry_days

    def test_a_later_bar_can_buy_again(self):
        s = Hooked({1, 5}, {4: "REBALANCE"}, stop=50.0)
        trades = run_engine(s, frame([100] * 9))
        assert len(trades) == 2
        assert pd.Timestamp(trades[1].entry_time) == frame([100] * 9).index[5]


class TestNoStopEntries:
    def test_an_entry_without_a_stop_is_accepted(self):
        s = Hooked({2}, {5: "REBALANCE"}, stop=None)
        (t,) = run_engine(s, frame([100, 100, 100, 100, 100, 110, 110]))
        assert t.reason == "REBALANCE"

    def test_risk_and_r_are_undefined_not_invented(self):
        s = Hooked({2}, {5: "REBALANCE"}, stop=None)
        (t,) = run_engine(s, frame([100, 100, 100, 100, 100, 110, 110]))
        assert math.isnan(t.risk)
        assert math.isnan(t.r_gross) and math.isnan(t.r_net)
        assert not t.r_defined

    def test_returns_are_carried_as_fractions_and_bps(self):
        s = Hooked({2}, {5: "REBALANCE"}, stop=None)
        (t,) = run_engine(s, frame([100, 100, 100, 100, 100, 110, 110]), cost_bps=24.0)
        assert t.gross_ret == pytest.approx(0.10)
        assert t.net_ret == pytest.approx(0.10 - 0.0024)
        assert t.gross_bps == pytest.approx(1000.0)
        assert t.net_bps == pytest.approx(976.0)

    def test_no_stop_means_no_stop_exit_however_far_it_falls(self):
        opens = [100, 100, 100, 50, 40, 30, 30]
        closes = [100, 100, 100, 50, 40, 30, 30]
        s = Hooked({1}, {}, stop=None)
        (t,) = run_engine(s, frame(opens, closes), max_hold_bars=None)
        assert t.reason == "EOD_DATA"
        assert t.exit == pytest.approx(30.0)

    def test_a_stop_based_trade_keeps_its_r_and_its_bps_bridge(self):
        s = Hooked({2}, {5: "REBALANCE"}, stop=95.0)
        (t,) = run_engine(s, frame([100, 100, 100, 100, 100, 110, 110]), cost_bps=24.0)
        assert t.r_defined
        assert t.r_gross == pytest.approx(2.0)
        assert t.gross_bps == pytest.approx(t.r_gross * t.risk_pct * 100)
        assert t.net_bps == pytest.approx(t.r_net * t.risk_pct * 100)

    def test_a_no_stop_signal_is_recorded_as_accepted_with_no_stop_value(self):
        from signal_engine.backtest.ledgers import LedgerRecorder

        rec = LedgerRecorder()
        s = Hooked({2}, {5: "REBALANCE"}, stop=None)
        df = frame([100, 100, 100, 100, 100, 110, 110])
        trades = simulate_swing(Ctx(df, "A"), s, None, SwingRunConfig(), rec=rec)
        frames = rec.to_frames()
        sig = frames["signals"]
        assert len(sig) == 1 and bool(sig["accepted"].iloc[0])
        assert math.isnan(float(sig["stop"].iloc[0]))
        assert rec.reconcile(frames, trades) == []


class TestHoldingCap:
    OPENS = list(np.linspace(100, 130, 300))

    def test_the_default_cap_still_closes_at_120_bars(self):
        s = Hooked({1}, {}, stop=1.0)
        (t,) = run_engine(s, frame(self.OPENS))
        assert t.reason == "MAX_HOLD"

    def test_none_means_no_cap(self):
        s = Hooked({1}, {}, stop=1.0)
        (t,) = run_engine(s, frame(self.OPENS), max_hold_bars=None)
        assert t.reason == "EOD_DATA"
        assert pd.Timestamp(t.exit_time) == frame(self.OPENS).index[-1]

    def test_the_config_default_is_unchanged(self):
        assert SwingRunConfig().max_hold_bars == 120


class TestExitAtOpenIsRecorded:
    def test_the_trade_says_it_exited_at_the_open(self):
        s = Hooked({2}, {5: "REBALANCE"}, stop=90.0)
        (t,) = run_engine(s, frame([100, 101, 102, 103, 104, 110, 111, 112]))
        assert t.exit_at_open is True

    def test_a_close_exit_does_not(self):
        plain = SwingScript({2: (1, 90.0, math.inf)})
        (t,) = run_engine(plain, frame([100] * 8))
        assert t.exit_at_open is False
