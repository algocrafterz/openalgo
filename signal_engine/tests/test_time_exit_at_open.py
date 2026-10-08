"""Spec item 19: the time exit is the OPEN of the first bar at or after 14:45.

The 14:40 bar is the last bar managed normally (stop first, then TP at the close). The 14:45 bar is
not evaluated for stop or target. With no bar in the window the close is the DATA_GAP exception.
"""

import asyncio

import pandas as pd
import pytest

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.models import Direction, Signal
from signal_engine.pipeline.sim_sink import SimRules, SimSink
from signal_engine.strategy_core import trade_policy as tp
from signal_engine.strategy_core.exits import TP1_ONLY

IST = "Asia/Kolkata"
TE, SQ = 14 * 60 + 45, 15 * 60


class TestVerdict:
    @pytest.mark.parametrize(
        "bar,expected",
        [(14 * 60 + 40, ""), (14 * 60 + 45, "TIME_EXIT"), (14 * 60 + 55, "TIME_EXIT"),
         (15 * 60, "DATA_GAP"), (15 * 60 + 10, "DATA_GAP")],
    )
    def test_table(self, bar, expected):
        assert tp.time_exit_verdict(bar, TE, SQ) == expected


def frame(rows, times):
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-06-04 {t}", tz=IST) for t in times])
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000.0
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


class ArmAt(Strategy):
    name = "arm-at"

    def __init__(self, at=0):
        self.at = at

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        if i == self.at and direction == 1:
            return EntrySignal(direction=1, sl=98.0, tp=104.0, tag="T", meta={"targets": (104.0,)})
        return None


def cfg(**kw):
    base = {"skip_open_minutes": 0, "entry_cutoff_min": 15 * 60 + 30, "time_exit_min": TE,
            "max_trades_per_day": 9, "min_sl_pct": 0.0, "cost_bps": 0.0, "exit_plan": TP1_ONLY,
            "time_exit_at_open": True}
    base.update(kw)
    return RunConfig(**base)


def engine(rows, times, run, arm=0):
    return simulate(Ctx(frame(rows, times), "TEST"), ArmAt(arm), None, run)


def sink(rows, times, run, arm=0):
    s = SimSink(SimRules.from_run_config(run, 5), interval_min=5)
    for i, (ts, row) in enumerate(frame(rows, times).iterrows()):
        asyncio.run(s.on_bar("TEST", ts, row.Open, row.High, row.Low, row.Close))
        asyncio.run(s.after_bar("TEST", ts, row.Open, row.High, row.Low, row.Close))
        if i == arm:
            asyncio.run(s.submit(Signal(
                strategy="BREAKOUT", direction=Direction.LONG, symbol="TEST", entry=row.Close,
                sl=98.0, tp=104.0, exchange="NSE", product="MIS", sig_id="T-0",
                context={"trigger": "T"})))
    s.flush()
    return s.trades


# bar 0 (14:35) arms at 100; bar 1 (14:40) fills at its open 100 and drifts quietly
TIMES = ["14:35", "14:40", "14:45", "14:50"]
QUIET = [(100, 101, 99.5, 100), (100, 101, 99.5, 100.5)]


@pytest.mark.parametrize("mode", [engine, sink])
class TestTheBoundary:
    def test_the_position_closes_at_the_14_45_open(self, mode):
        # the 14:45 bar opens at 100.7 and then spans both the stop (97) and the target (105)
        rows = [*QUIET, (100.7, 105.0, 97.0, 99.0), (99.0, 99.5, 98.5, 99.0)]
        (t,) = mode(rows, TIMES, cfg())
        assert t.reason == "TIME_EXIT" and t.exit == 100.7

    def test_the_14_45_bar_is_not_evaluated_for_the_stop(self, mode):
        rows = [*QUIET, (100.7, 100.9, 90.0, 95.0), (95.0, 95.5, 94.5, 95.0)]
        (t,) = mode(rows, TIMES, cfg())
        assert t.reason == "TIME_EXIT" and t.exit == 100.7  # not the stop at 98

    def test_a_stop_inside_the_14_40_bar_is_a_normal_stop(self, mode):
        rows = [(100, 101, 99.5, 100), (100, 100.5, 97.5, 98.2), (98.2, 99, 97.5, 98.5), (98.5, 99, 98, 98.5)]
        (t,) = mode(rows, TIMES, cfg())
        assert t.reason == "SL" and t.exit == 98.0

    def test_with_the_rule_off_the_exit_is_the_older_close_of_the_14_45_bar(self, mode):
        rows = [*QUIET, (100.7, 100.9, 99.9, 100.2), (100.2, 100.5, 99.9, 100.3)]
        (t,) = mode(rows, TIMES, cfg(time_exit_at_open=False))
        assert t.reason == "TIME_EXIT" and t.exit == 100.2


@pytest.mark.parametrize("mode", [engine, sink])
class TestWhenTheBarIsMissing:
    # a normal run of 5-minute bars up to 14:40 (the engine reads the bar length from the data)
    PRE = ["14:20", "14:25", "14:30", "14:35", "14:40"]
    PRE_ROWS = [(100, 101, 99.5, 100)] * 3 + QUIET  # arms on the 14:35 bar (index 3), fills 14:40

    def test_the_first_bar_after_14_45_is_used(self, mode):
        rows = [*self.PRE_ROWS, (100.4, 100.9, 99.9, 100.2)]
        (t,) = mode(rows, [*self.PRE, "14:55"], cfg(), arm=3)
        assert t.reason == "TIME_EXIT" and t.exit == 100.4

    def test_no_bar_before_the_squareoff_is_a_flagged_data_gap_at_the_last_known_close(self, mode):
        rows = [*self.PRE_ROWS, (100.4, 100.9, 99.9, 100.2)]
        (t,) = mode(rows, [*self.PRE, "15:05"], cfg(), arm=3)
        assert t.reason == "DATA_GAP" and t.exit == 100.5  # the 14:40 close

    def test_a_session_that_ends_before_the_time_exit_is_a_data_gap_not_a_time_exit(self, mode):
        (t,) = mode(self.PRE_ROWS, self.PRE, cfg(), arm=3)
        assert t.reason == "DATA_GAP" and t.exit == 100.5


class TestTargetBeforeTheBoundary:
    def test_a_target_inside_the_14_40_bar_exits_at_that_bars_close(self):
        # the SimSink takes its target exits from the ExitDriver (covered by the trade-parity tests)
        rows = [(100, 101, 99.5, 100), (100, 105.0, 99.8, 103.2), (103.2, 104, 102, 103), (103, 104, 102, 103)]
        (t,) = engine(rows, TIMES, cfg())
        assert t.reason == "TP" and t.exit == 103.2


class TestAdapterCarriesTheFrozenRules:
    def test_run_overrides(self):
        from signal_engine.backtest.strategies.breakout import Breakout

        run = RunConfig().with_(**Breakout.run_overrides)
        assert run.time_exit_at_open is True and run.exit_plan is TP1_ONLY
        assert (run.max_entry_slippage_r, run.max_entries_per_direction) == (0.5, 2)
