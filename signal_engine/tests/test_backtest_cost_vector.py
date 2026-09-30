"""Cost sensitivity is recomputed from one trade set, not by re-simulating per cost level."""

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from signal_engine.backtest import indicators as ind
from signal_engine.backtest import metrics
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import EntrySignal, RunConfig

IST = "Asia/Kolkata"


class Fires(Strategy):
    tag = "FIRES"

    def prepare(self, df, p):
        d = ind.add_session_columns(df)
        d["fire"] = (np.arange(len(d)) % 17 == 5).astype(float)
        return d

    def entry(self, c, i, p, direction):
        if direction != 1 or not c["fire"][i]:
            return None
        px = float(c["Close"][i])
        return EntrySignal(1, px - 1.0, px + 1.5, "FIRES")


def _frames(seed=0, days=8):
    rng = np.random.default_rng(seed)
    out = {}
    for sym in ("A", "B"):
        parts = []
        for d in pd.bdate_range("2026-03-02", periods=days):
            idx = pd.date_range(f"{d.date()} 09:15", periods=70, freq="5min", tz=IST)
            close = 100 + np.cumsum(rng.normal(0, 0.4, len(idx)))
            open_ = np.r_[close[0], close[:-1]]
            parts.append(
                pd.DataFrame(
                    {
                        "Open": open_,
                        "High": np.maximum(open_, close) + 0.2,
                        "Low": np.minimum(open_, close) - 0.2,
                        "Close": close,
                        "Volume": 1000.0,
                    },
                    index=idx,
                )
            )
        out[sym] = pd.concat(parts)
    return out


RUN = RunConfig(
    skip_open_minutes=0,
    entry_cutoff_min=15 * 60,
    max_trades_per_day=99,
    one_trade_per_direction=False,
)


class TestWithFlatCost:
    def test_restates_net_from_gross(self):
        bt = Backtest(Fires(), _frames(), RUN)
        trades = bt.trades(object(), "all")
        restated = metrics.with_flat_cost(trades, 24.0)
        for old, new in zip(trades, restated, strict=True):
            assert new.r_net == pytest.approx(old.r_gross - 0.0024 * old.entry / old.risk)
            assert new.r_gross == old.r_gross

    def test_inputs_are_not_modified(self):
        bt = Backtest(Fires(), _frames(), RUN)
        trades = bt.trades(object(), "all")
        before = [t.r_net for t in trades]
        metrics.with_flat_cost(trades, 99.0)
        assert [t.r_net for t in trades] == before


class TestCostSensitivity:
    def test_matches_a_full_resimulation_at_each_level(self):
        bt = Backtest(Fires(), _frames(), RUN)
        levels = (0, 8, 16, 24)
        got = bt.cost_sensitivity(object(), bps=levels)
        expected = pd.DataFrame(
            [
                metrics.summary(
                    bt.trades(object(), "all", run=bt.run.with_(cost_bps=b)), f"{b:.0f} bps"
                )
                for b in levels
            ]
        )
        pdt.assert_frame_equal(got, expected)

    def test_runs_the_engine_once_however_many_levels(self):
        calls = []

        def counting(ctx, strategy, p, run):
            calls.append(1)
            return simulate(ctx, strategy, p, run)

        bt = Backtest(Fires(), _frames(), RUN, engine=counting)
        bt.cost_sensitivity(object(), bps=(0, 8, 16, 24, 35))
        assert len(calls) == 2  # one pass over the two symbols, not five
