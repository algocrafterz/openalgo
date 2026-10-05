"""IDEA-028, lowest realised volatility: the strategy class against its frozen spec."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from signal_engine.backtest import __main__ as cli
from signal_engine.backtest import data, panel
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.session import derive_calendar
from signal_engine.backtest.strategies.low_volatility import (
    LowVolatility,
    LowVolatilityParams,
    return_volatility,
)
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing

SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "ideas"
    / "idea-028-low-volatility"
    / "spec.yaml"
)


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))


def frames_from(closes: dict[str, list[float]], start="2026-01-01"):
    """Daily frames whose open equals the previous close; NaN closes are left out (no bar)."""
    n = len(next(iter(closes.values())))
    idx = pd.bdate_range(start, periods=n, tz="Asia/Kolkata")
    out = {}
    for sym, series in closes.items():
        close = np.asarray(series, dtype=float)
        open_ = np.concatenate([[close[0]], close[:-1]])
        raw = pd.DataFrame(
            {
                "Open": open_,
                "High": np.fmax(open_, close) * 1.001,
                "Low": np.fmin(open_, close) * 0.999,
                "Close": close,
                "Volume": 10_000.0,
            },
            index=idx,
        ).dropna()
        out[sym] = data._downcast(data._split_adjust_daily(raw))
    return out


def wobble(n, amplitude, start=100.0, seed=0):
    """A series that moves up or down by `amplitude` percent each session, with a small drift."""
    rng = np.random.default_rng(seed)
    signs = rng.choice([-1.0, 1.0], size=n)
    return list(start * np.cumprod(1 + signs * amplitude / 100 + 0.0005))


def schedule_for(frames, params):
    calendar = derive_calendar(frames)
    prepared = LowVolatility().prepare_panel(frames, params, calendar)
    return prepared, calendar


def run_strategy(closes, params):
    frames = frames_from(closes)
    strategy = LowVolatility()
    prepared = strategy.prepare_panel(frames, params, derive_calendar(frames))
    run = SwingRunConfig(cost_bps=24.0, max_hold_bars=None)
    bt = Backtest(strategy, prepared, run, engine=simulate_swing, release_raw=False)
    return frames, bt.trades(params, "all")


SMALL = LowVolatilityParams(vol_lookback_returns=5, max_positions=2)


class TestBindingToTheFrozenSpec:
    def test_the_class_defaults_are_exactly_the_specs_parameters(self, spec):
        rr.check_parameter_binding(spec["parameters"], rr.jsonable(LowVolatilityParams()))

    def test_the_rules_the_spec_states_are_the_ones_the_parameters_encode(self, spec):
        p = LowVolatilityParams()
        assert (p.vol_lookback_returns, p.max_positions) == (252, 30)
        assert (p.rebalance, p.rebalance_day) == ("monthly", "last_session_of_month")
        assert spec["portfolio"]["max_concurrent"] == p.max_positions
        assert spec["indicators"] == [{"name": "RETURN_STDEV", "period": 252, "source": "close"}]
        assert spec["direction"] == "long_only" and spec["stop"] == "none"
        assert spec["take_profit"] == "none" and spec["holding_period"] == "none"
        assert spec["universe"]["type"] == "current_fno"

    def test_it_is_registered_as_a_swing_strategy(self):
        cli.REGISTRY.clear()
        cli._register()
        assert cli.REGISTRY["low_volatility"] == (LowVolatility, LowVolatilityParams, "swing")
        assert "low_volatility" in rr.supported_keys()

    def test_only_the_monthly_last_session_rebalance_is_supported(self):
        frames = frames_from({"A": wobble(40, 1.0)})
        calendar = derive_calendar(frames)
        for bad in (LowVolatilityParams(rebalance="weekly"),
                    LowVolatilityParams(rebalance_day="first_session_of_month")):
            with pytest.raises(ValueError, match="supports only"):
                LowVolatility().prepare_panel(frames, bad, calendar)


class TestVolatilityDefinition:
    def _close(self, values):
        idx = pd.DatetimeIndex(pd.bdate_range("2026-01-01", periods=len(values)))
        return pd.DataFrame({"A": values}, index=idx)

    def test_it_is_the_sample_std_of_simple_returns_not_annualised(self):
        values = [100.0, 102.0, 99.0, 101.0, 105.0, 104.0]
        vol = return_volatility(self._close(values), 5)
        r = np.array(values[1:]) / np.array(values[:-1]) - 1
        assert vol["A"].iloc[-1] == pytest.approx(np.std(r, ddof=1), rel=1e-12)

    def test_the_first_value_needs_n_plus_one_consecutive_closes(self):
        values = [100.0, 102.0, 99.0, 101.0, 105.0, 104.0, 103.0]
        vol = return_volatility(self._close(values), 5)["A"]
        # five returns need six closes: index 4 has five closes, index 5 has six
        assert vol.iloc[:5].isna().all() and not np.isnan(vol.iloc[5])

    def test_a_missing_session_is_never_compressed_into_one_return(self):
        values = [100.0, 102.0, 99.0, np.nan, 101.0, 105.0, 104.0, 103.0, 106.0, 107.0, 108.0]
        vol = return_volatility(self._close(values), 5)["A"]
        # returns at index 3 and 4 touch the gap; five consecutive returns resume at index 9
        assert vol.iloc[:9].isna().all() and not np.isnan(vol.iloc[9])

    def test_fewer_than_two_returns_is_refused(self):
        with pytest.raises(ValueError):
            return_volatility(self._close([100.0, 101.0, 102.0]), 1)


class TestSelection:
    def test_the_lowest_volatility_names_are_held(self):
        n = 45
        frames = frames_from({"CALM": wobble(n, 0.2, seed=1), "MID": wobble(n, 1.0, seed=2),
                              "WILD": wobble(n, 3.0, seed=3)})
        prepared, _ = schedule_for(frames, SMALL)
        jan_rebalance_exec = prepared["CALM"].index.date.tolist().index(date(2026, 2, 2))
        assert prepared["CALM"]["target_prev"].iloc[jan_rebalance_exec]
        assert prepared["MID"]["target_prev"].iloc[jan_rebalance_exec]
        assert not prepared["WILD"]["target_prev"].iloc[jan_rebalance_exec]

    def test_identical_input_gives_identical_selection_and_ties_use_the_platform_hash(self):
        n = 45
        same = wobble(n, 1.0, seed=7)
        closes = {"AAA": same, "BBB": same, "CCC": same}
        a, _ = schedule_for(frames_from(closes), SMALL)
        b, _ = schedule_for(frames_from(closes), SMALL)
        day = date(2026, 1, 30)
        held = {s for s, f in a.items() if f["target_latest"].loc[f.index.date > day].iloc[0]}
        again = {s for s, f in b.items() if f["target_latest"].loc[f.index.date > day].iloc[0]}
        expected = sorted(closes, key=lambda s: panel._tie_break(s, day))[:2]
        assert held == again == set(expected)


class TestExecution:
    def test_orders_fill_at_the_next_sessions_open(self):
        closes = {"CALM": wobble(45, 0.2, seed=1), "WILD": wobble(45, 3.0, seed=3)}
        frames, trades = run_strategy(closes, LowVolatilityParams(vol_lookback_returns=5,
                                                                  max_positions=1))
        first = min(trades, key=lambda t: t.entry_time)
        assert first.symbol == "CALM"
        assert pd.Timestamp(first.entry_time).date() == date(2026, 2, 2)  # after Fri 30 Jan
        bar = frames["CALM"].loc[frames["CALM"].index.date == date(2026, 2, 2)].iloc[0]
        assert first.entry == pytest.approx(float(bar["Open"]), rel=1e-6)

    def test_a_held_stock_that_stays_selected_is_not_sold_and_bought_again(self):
        closes = {"CALM": wobble(90, 0.2, seed=1), "WILD": wobble(90, 3.0, seed=3)}
        _, trades = run_strategy(closes, LowVolatilityParams(vol_lookback_returns=5,
                                                             max_positions=1))
        calm = [t for t in trades if t.symbol == "CALM"]
        assert len(calm) == 1  # bought once at the first rebalance, held through the next ones


class TestNoLookahead:
    def test_a_rebalance_decision_ignores_every_later_price(self):
        n = 45
        base = {"CALM": wobble(n, 0.2, seed=1), "MID": wobble(n, 1.0, seed=2),
                "WILD": wobble(n, 3.0, seed=3)}
        day = date(2026, 1, 30)
        cut = pd.bdate_range("2026-01-01", periods=n).date.tolist().index(day)
        changed = {s: v[: cut + 1] + [x * (5.0 if s == "WILD" else 0.2) for x in v[cut + 1:]]
                   for s, v in base.items()}
        a, _ = schedule_for(frames_from(base), SMALL)
        b, _ = schedule_for(frames_from(changed), SMALL)
        for s in base:
            fa, fb = a[s], b[s]
            mask = fa.index.date <= date(2026, 2, 2)  # through the execution session
            assert fa.loc[mask, "target_prev"].tolist() == fb.loc[mask, "target_prev"].tolist()

    def test_the_decision_is_not_visible_on_the_rebalance_session_itself(self):
        frames = frames_from({"CALM": wobble(45, 0.2, seed=1), "WILD": wobble(45, 3.0, seed=3)})
        prepared, _ = schedule_for(frames, SMALL)
        f = prepared["CALM"]
        on_day = f.loc[f.index.date == date(2026, 1, 30)].iloc[0]
        assert not on_day["target_latest"] and not on_day["target_prev"]
