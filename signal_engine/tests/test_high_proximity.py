"""IDEA-027, proximity to the 52-week high: the strategy class against its frozen spec."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from signal_engine.backtest import __main__ as cli
from signal_engine.backtest import data, panel
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import truncation as tr
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.session import derive_calendar
from signal_engine.backtest.strategies.high_proximity import HighProximity, HighProximityParams
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing

SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "ideas"
    / "idea-027-52-week-high-proximity"
    / "spec.yaml"
)


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def frames_from(closes: dict[str, list[float]], start="2026-01-05"):
    """Daily frames whose open equals the previous close (no gap), so fills are easy to predict."""
    n = len(next(iter(closes.values())))
    idx = pd.bdate_range(start, periods=n, tz="Asia/Kolkata")
    out = {}
    for sym, series in closes.items():
        close = np.asarray(series, dtype=float)
        open_ = np.concatenate([[close[0]], close[:-1]])
        raw = pd.DataFrame(
            {
                "Open": open_,
                "High": np.maximum(open_, close) * 1.001,
                "Low": np.minimum(open_, close) * 0.999,
                "Close": close,
                "Volume": 10_000.0,
            },
            index=idx,
        )
        out[sym] = data._downcast(data._split_adjust_daily(raw))
    return out


def run_strategy(closes, params):
    frames = frames_from(closes)
    strategy = HighProximity()
    calendar = derive_calendar(frames)
    prepared = strategy.prepare_panel(frames, params, calendar)
    run = SwingRunConfig(cost_bps=24.0, max_hold_bars=None)
    bt = Backtest(strategy, prepared, run, engine=simulate_swing, release_raw=False)
    return frames, bt.trades(params, "all")


SMALL = HighProximityParams(lookback_sessions=10, max_positions=3)


def rising(n, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


class TestBindingToTheFrozenSpec:
    def test_the_class_defaults_are_exactly_the_specs_parameters(self, spec):
        rr.check_parameter_binding(spec["parameters"], rr.jsonable(HighProximityParams()))

    def test_the_rules_the_spec_states_are_the_ones_the_parameters_encode(self, spec):
        p = HighProximityParams()
        assert (p.lookback_sessions, p.closeness_pct, p.max_positions) == (252, 5, 20)
        assert (p.rebalance, p.rebalance_day) == ("weekly", "last_session_of_week")
        assert spec["portfolio"]["max_concurrent"] == p.max_positions
        assert spec["direction"] == "long_only" and spec["stop"] == "none"
        assert spec["take_profit"] == "none" and spec["holding_period"] == "none"

    def test_it_is_registered_as_a_swing_strategy(self):
        cli.REGISTRY.clear()
        cli._register()
        cls, params_cls, kind = cli.REGISTRY["high_proximity"]
        assert (cls, params_cls, kind) == (HighProximity, HighProximityParams, "swing")
        assert "high_proximity" in rr.supported_keys()

    def test_only_the_weekly_last_session_rebalance_is_supported(self):
        strategy = HighProximity()
        frames = frames_from({"A": rising(30)})
        calendar = derive_calendar(frames)
        for bad in (
            HighProximityParams(rebalance="monthly"),
            HighProximityParams(rebalance_day="monday"),
        ):
            with pytest.raises(ValueError, match="supports only"):
                strategy.prepare_panel(frames, bad, calendar)


class TestSelection:
    # A rises to a high of 200 on session 9 (the first rebalance), then closes at a fixed level.
    # At the second rebalance (session 14) its 10-session high is still 200. Whether it is held
    # after that rebalance shows whether the 5 percent boundary is inclusive.
    def a_after_the_high(self, level):
        a = rising(9, 100, 12.5) + [200.0] + [level] * 9
        _, trades = run_strategy({"A": a, "B": [100.0] * 19}, SMALL)
        return [t for t in trades if t.symbol == "A"]

    def test_a_stock_exactly_five_percent_below_its_high_stays_in_the_book(self):
        (trade,) = self.a_after_the_high(190.0)  # 190 / 200 = 0.95
        assert trade.reason == "EOD_DATA"  # held through the second rebalance, not sold

    def test_a_stock_just_beyond_five_percent_is_sold_at_the_next_open(self):
        (trade,) = self.a_after_the_high(189.9)  # 0.9495
        assert trade.reason == "REBALANCE_EXIT" and trade.exit_at_open

    def test_only_the_closest_max_positions_are_held(self):
        closes = {f"S{i:02d}": rising(19, 100, 1.0) for i in range(5)}
        # all five trend up to a new high every day (ratio 1.0): the cap of 3 decides
        _, trades = run_strategy(closes, SMALL)
        first_entry = min(pd.Timestamp(t.entry_time) for t in trades)
        opened = [t for t in trades if pd.Timestamp(t.entry_time) == first_entry]
        assert len(opened) == 3

    def test_fewer_qualifiers_means_fewer_positions_and_idle_cash(self):
        closes = {"A": rising(19, 100, 1.0), "B": [100.0 - i for i in range(19)]}
        _, trades = run_strategy(closes, SMALL)
        assert {t.symbol for t in trades} == {"A"}

    def test_the_closest_to_the_high_is_preferred_when_over_the_cap(self):
        n = 19
        near = rising(n, 100, 1.0)  # at its high every day
        # same trend but each close lags: ratio < 1 while still within 5 percent
        lag = [100 + i - 2.0 for i in range(n)]
        closes = {"A": near, "B": lag, "C": [c - 3.5 for c in near], "D": [c - 4.0 for c in near]}
        params = HighProximityParams(lookback_sessions=10, max_positions=2)
        _, trades = run_strategy(closes, params)
        held_first = {
            t.symbol
            for t in trades
            if pd.Timestamp(t.entry_time) == min(pd.Timestamp(x.entry_time) for x in trades)
        }
        assert "A" in held_first and len(held_first) == 2


class TestExecution:
    def test_orders_fill_at_the_next_sessions_open_and_never_at_the_signal_close(self):
        closes = {"A": rising(19, 100, 1.0), "B": [100.0] * 19}
        frames, trades = run_strategy(closes, SMALL)
        for t in trades:
            row = frames[t.symbol].loc[pd.Timestamp(t.entry_time)]
            assert t.entry == pytest.approx(float(row["Open"]), rel=1e-6)

    def test_there_is_no_stop_no_target_and_no_r(self):
        _, trades = run_strategy({"A": rising(19, 100, 1.0), "B": [100.0] * 19}, SMALL)
        assert trades
        assert all(math.isnan(t.sl) and not t.r_defined for t in trades)

    def test_a_stock_that_drops_out_is_sold_at_the_next_open_with_the_rebalance_reason(self):
        a = rising(11, 100, 1.0) + [60.0] * 10  # collapses after the second rebalance
        b = rising(21, 100, 1.0)
        _, trades = run_strategy({"A": a, "B": b}, SMALL)
        sold = [t for t in trades if t.symbol == "A" and t.reason == "REBALANCE_EXIT"]
        assert sold and all(t.exit_at_open for t in sold)

    def test_a_held_stock_that_stays_qualified_is_not_sold_and_not_rebought(self):
        _, trades = run_strategy({"A": rising(40, 100, 1.0), "B": [100.0] * 40}, SMALL)
        a_trades = [t for t in trades if t.symbol == "A"]
        assert len(a_trades) == 1  # one holding episode across many weekly rebalances

    def test_a_stock_can_be_bought_again_after_it_was_sold(self):
        a = rising(11, 100, 1.0) + [40.0] * 8 + rising(21, 41, 6.0)
        b = [100.0] * 40
        _, trades = run_strategy({"A": a, "B": b}, SMALL)
        assert len([t for t in trades if t.symbol == "A"]) >= 2

    def test_there_is_no_maximum_holding_period(self):
        _, trades = run_strategy({"A": rising(150, 100, 1.0), "B": [100.0] * 150}, SMALL)
        (a,) = [t for t in trades if t.symbol == "A"]
        assert a.reason == "EOD_DATA"  # held until the data ended, not cut at 120 bars


class TestTheTwoFiftyTwoSessionRule:
    def test_nothing_is_traded_before_a_full_252_sessions_of_history(self):
        closes = {"A": rising(240, 100, 0.5), "B": [100.0] * 240}
        _, trades = run_strategy(closes, HighProximityParams(max_positions=3))
        assert trades == []

    def test_the_first_signal_comes_after_252_sessions(self):
        n = 300
        closes = {"A": rising(n, 100, 0.5), "B": [100.0] * n}
        frames, trades = run_strategy(closes, HighProximityParams(max_positions=3))
        assert trades
        first = min(pd.Timestamp(t.entry_time) for t in trades)
        index = frames["A"].index
        assert index.get_loc(first) >= 252  # the 252nd session is index 251; entry is after it

    def test_a_calendar_window_not_a_row_window_is_used(self):
        # a symbol with sessions removed has a shorter row history but the same calendar window
        n = 300
        closes = {"A": rising(n, 100, 0.5), "B": [100.0] * n}
        frames = frames_from(closes)
        gap = frames["A"].drop(frames["A"].index[100:110])  # ten sessions removed mid-history
        calendar = derive_calendar(frames)
        sessions = list(calendar.trading_days)
        close = panel.close_panel({"A": gap}, sessions)
        _, ok = panel.trailing_high(close, 252)
        assert ok["A"].tolist().index(True) == 251  # not 261: rows removed do not delay it


class TestNoLookahead:
    def test_the_ranking_reproduces_its_earlier_trades_on_truncated_data(self):
        rng = np.random.default_rng(5)
        closes = {}
        for k in range(8):
            r = rng.normal(0.003 if k % 2 == 0 else -0.001, 0.012, 150)
            closes[f"S{k}"] = list(100 * np.exp(np.cumsum(r)))
        frames = frames_from(closes)
        strategy, params = (
            HighProximity(),
            HighProximityParams(lookback_sessions=20, max_positions=3),
        )

        def panel_fn(fr):
            return strategy.prepare_panel(fr, params, derive_calendar(fr))

        result = tr.truncation_check(
            strategy,
            params,
            frames,
            SwingRunConfig(cost_bps=24.0, max_hold_bars=None),
            engine=simulate_swing,
            symbols=4,
            panel=panel_fn,
        )
        assert result.ok, result.mismatches
        assert result.checked

    def test_the_decision_columns_are_visible_only_after_the_rebalance_session(self):
        frames = frames_from({"A": rising(30, 100, 1.0), "B": [100.0] * 30})
        calendar = derive_calendar(frames)
        out = HighProximity().prepare_panel(frames, SMALL, calendar)["A"]
        first = panel.weekly_rebalance_sessions(list(calendar.trading_days))[0]
        row = out.loc[pd.Timestamp(first).tz_localize("Asia/Kolkata")]
        # nothing decided before the first rebalance, and its own decision is not yet visible
        assert not row["target_latest"] and not row["rebalance_prev"] and not row["target_prev"]
        after = out.iloc[out.index.get_loc(row.name) + 1]
        assert after["rebalance_prev"]  # the next session is the execution session
