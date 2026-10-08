"""Spec item 12: a touched TP1 exits at the CLOSE of the touching bar (owner decision 2026-10-07).

The stop is still checked first: a bar that reaches both takes the stop. The three-leg ladder keeps
its target-price fills (tp_fill="target"); only the v1.0 plan tp1_only uses the bar close.
"""

import pandas as pd
import pytest

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.pipeline.exit_driver import PLANS, plan_for
from signal_engine.strategy_core.exits import ExitPlan, Ladder

IST = "Asia/Kolkata"


class TestLadderFill:
    def test_tp1_only_fills_at_the_close_of_the_touching_bar(self):
        lad = Ladder(1, 100.0, 98.0, (104.0,), PLANS["tp1_only"])
        lad.step(100.0, 105.0, 99.5, 103.0)  # touches 104, closes at 103
        assert lad.done and lad.fills[-1].price == 103.0 and lad.fills[-1].reason == "TP1"

    def test_the_close_can_be_above_the_target_too(self):
        lad = Ladder(1, 100.0, 98.0, (104.0,), PLANS["tp1_only"])
        lad.step(100.0, 106.0, 99.5, 105.5)
        assert lad.fills[-1].price == 105.5

    def test_a_short_target_fills_at_the_close(self):
        lad = Ladder(-1, 100.0, 102.0, (96.0,), PLANS["tp1_only"])
        lad.step(100.0, 100.5, 95.0, 97.0)
        assert lad.fills[-1].price == 97.0

    def test_a_bar_that_reaches_the_stop_and_the_target_takes_the_stop(self):
        lad = Ladder(1, 100.0, 98.0, (104.0,), PLANS["tp1_only"])
        lad.step(100.0, 105.0, 97.5, 103.0)
        assert lad.fills[-1].reason == "SL" and lad.fills[-1].price == 98.0

    def test_a_bar_that_does_not_touch_the_target_does_nothing(self):
        lad = Ladder(1, 100.0, 98.0, (104.0,), PLANS["tp1_only"])
        lad.step(100.0, 103.9, 99.0, 103.5)
        assert not lad.done

    def test_the_three_leg_ladder_still_fills_at_the_target_price(self):
        lad = Ladder(1, 100.0, 98.0, (104.0, 108.0, 112.0), PLANS["ladder_30_35_35"])
        lad.step(100.0, 105.0, 99.5, 103.0)
        assert lad.fills[0].price == 104.0

    def test_the_close_is_required_under_bar_close(self):
        lad = Ladder(1, 100.0, 98.0, (104.0,), PLANS["tp1_only"])
        with pytest.raises(ValueError, match="bar close"):
            lad.step(100.0, 105.0, 99.5)

    def test_an_unknown_fill_mode_is_refused(self):
        with pytest.raises(ValueError, match="tp_fill"):
            ExitPlan((1.0,), "none", tp_fill="midpoint")

    def test_the_v1_0_plan_is_bar_close(self):
        assert plan_for("tp1_only").tp_fill == "bar_close"
        assert plan_for("ladder_30_35_35").tp_fill == "target"


def frame(rows):
    idx = pd.date_range("2026-06-04 09:15", periods=len(rows), freq="5min", tz=IST)
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000.0
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


class ArmOnce(Strategy):
    name = "arm-once"

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        if i == 1 and direction == 1:
            return EntrySignal(direction=1, sl=98.0, tp=104.0, tag="T", meta={"targets": (104.0,)})
        return None


class TestEngine:
    RUN = RunConfig(skip_open_minutes=0, entry_cutoff_min=15 * 60, time_exit_min=15 * 60,
                    max_trades_per_day=9, min_sl_pct=0.0, cost_bps=0.0, exit_plan=PLANS["tp1_only"])
    # bar 1 arms at 100; bar 2 fills at its open 100; bar 3 touches 104 (high 105) and closes at 103
    ROWS = [(100, 101, 99.5, 100), (100, 101, 99.5, 100), (100, 101, 99.5, 100),
            (100.5, 105, 100, 103), (103, 104, 102.5, 103.5)]

    def test_the_trade_exits_at_the_close_of_the_touching_bar(self):
        (t,) = simulate(Ctx(frame(self.ROWS), "TEST"), ArmOnce(), None, self.RUN)
        assert t.reason == "TP" and t.exit == 103.0
        assert t.r_gross == pytest.approx(3.0 / 2.0)

    def test_the_older_plan_without_the_close_rule_fills_at_the_target(self):
        run = self.RUN.with_(exit_plan=ExitPlan((1.0,), "none"))
        (t,) = simulate(Ctx(frame(self.ROWS), "TEST"), ArmOnce(), None, run)
        assert t.exit == 104.0
