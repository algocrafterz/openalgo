"""The backtest adapter is a thin wrapper over strategy_core: it holds no strategy rules.

The core's input builder is patched with the PDH-retest scenario, so these tests pin the
WIRING (which bars fire, with which stop/target/tag) and that the shared engine can run it.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.strategies import breakout as adapter_mod
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.strategies.breakout import Breakout, BreakoutParams
from signal_engine.backtest.types import Ctx, RunConfig
from signal_engine.strategy_core.breakout import BreakoutParams as CoreParams

IST = "Asia/Kolkata"
BARS = [
    (99.5, 99.8, 99.2, 1.0),
    (100.6, 100.7, 100.4, 1.0),
    (100.5, 100.7, 99.9, 2.0),
    (100.5, 100.7, 99.9, 2.0),
]
COLS = {
    "atr": 1.0,
    "vwap": 99.0,
    "ema9": 99.5,
    "ema_slope": 0.1,
    "sess_vf": 1.5,
    "htf_close": np.nan,
    "pdh": 100.0,
    "pdl": np.nan,
    "ib_high": 101.0,
    "ib_low": 95.0,
    "ib_done": True,
    "orb_high": np.nan,
    "orb_low": np.nan,
    "pvah": np.nan,
    "ppoc": np.nan,
    "pval": np.nan,
    "adr": 5.0,
    "day_high": 101.0,
    "day_low": 99.0,
}


def raw_bars() -> pd.DataFrame:
    idx = pd.date_range("2026-09-29 10:00", periods=len(BARS), freq="5min", tz=IST)
    close = [b[0] for b in BARS]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [b[1] for b in BARS],
            "Low": [b[2] for b in BARS],
            "Close": close,
            "Volume": 1000.0,
        },
        index=idx,
    )


def scenario_inputs(*_args, **_kwargs) -> pd.DataFrame:
    raw = raw_bars()
    df = pd.DataFrame(COLS, index=raw.index)
    df["Close"], df["High"], df["Low"] = raw["Close"], raw["High"], raw["Low"]
    df["rvol"] = [b[3] for b in BARS]
    return df


@pytest.fixture
def strat(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_breakout_inputs", scenario_inputs)
    return Breakout(minute_source=lambda sym, df5: raw_bars(), tick_source=lambda sym: 0.05)


def prepared(strat, p=None):
    p = p or BreakoutParams()
    return Ctx(strat.prepare_for("SYM", raw_bars(), p), symbol="SYM"), p


class TestWiring:
    def test_params_are_the_cores_single_definition(self):
        assert BreakoutParams is CoreParams

    def test_entry_returns_the_cores_stop_target_and_tag_on_the_fire_bar_only(self, strat):
        c, p = prepared(strat)
        sig = strat.entry(c, 2, p, 1)
        assert sig is not None
        assert (sig.direction, sig.tag) == (1, "PDH-RT")
        assert sig.sl == pytest.approx(99.65, abs=1e-3)
        assert sig.tp == pytest.approx(101.35, abs=1e-3)
        assert [strat.entry(c, i, p, 1) for i in (0, 1, 3)] == [None, None, None]

    def test_signal_carries_the_cores_three_targets_for_the_shared_exit_model(self, strat):
        c, p = prepared(strat)
        tp1, tp2, tp3 = strat.entry(c, 2, p, 1).meta["targets"]
        assert tp1 == pytest.approx(101.35, abs=1e-3)
        assert tp1 < tp2 < tp3
        assert tp3 == pytest.approx(100.5 + 3 * 0.85, abs=1e-3)

    def test_wrong_direction_gets_no_signal(self, strat):
        c, p = prepared(strat)
        assert strat.entry(c, 2, p, -1) is None

    def test_core_params_flow_through(self, strat):
        c, p = prepared(strat, BreakoutParams(score_threshold=11))
        assert strat.entry(c, 2, p, 1) is None  # the retest scores 10

    def test_the_core_owns_the_entry_window(self):
        assert Breakout.run_overrides["skip_open_minutes"] == 0
        assert Breakout.run_overrides["entry_cutoff_min"] >= 15 * 60


class TestSharedEngine:
    def test_runs_end_to_end_through_the_shared_engine(self, strat):
        run = RunConfig().with_(**Breakout.run_overrides)
        trades = Backtest(strat, {"SYM": raw_bars()}, run).trades(BreakoutParams(), "all")
        assert len(trades) == 1
        t = trades[0]
        assert (t.symbol, t.tag, t.direction) == ("SYM", "PDH-RT", 1)
        assert t.sl == pytest.approx(99.65, abs=1e-3)


class TestBaseHook:
    def test_prepare_for_defaults_to_prepare_so_other_adapters_are_unchanged(self):
        class Dummy(Strategy):
            seen = None

            def prepare(self, df, p):
                Dummy.seen = "prepare"
                return df

            def entry(self, c, i, p, direction):
                return None

        out = Dummy().prepare_for("ANY", raw_bars(), object())
        assert Dummy.seen == "prepare"
        assert len(out) == len(BARS)
