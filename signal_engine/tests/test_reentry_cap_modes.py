"""Spec item 15 in the backtest engine and the SimSink: LONG, exit, LONG is allowed, a third is not.

One scripted sequence goes through both modes with the same RunConfig and must give the same trades.
(The paper and live path is covered by test_entry_trade_policy.py; the same function decides in all
of them: strategy_core.trade_policy.)
"""

import asyncio

import pandas as pd

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.strategies.breakout import Breakout
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.models import Direction, Signal
from signal_engine.pipeline.sim_sink import SimRules, SimSink

IST = "Asia/Kolkata"
# bar i: (open, high, low, close). A long armed at the close of bar k fills at the open of bar k+1.
# Bars 1, 4, 7 arm a long (sl 98, tp 110); bars 3 and 6 stop it out (low 97).
ROWS = [
    (100, 101, 99.5, 100),  # 0
    (100, 101, 99.5, 100),  # 1 arm 1
    (100, 101, 99.5, 100),  # 2 fill 1
    (100, 101, 97.0, 98),   # 3 stopped out
    (100, 101, 99.5, 100),  # 4 arm 2 (the position is flat again)
    (100, 101, 99.5, 100),  # 5 fill 2
    (100, 101, 97.0, 98),   # 6 stopped out
    (100, 101, 99.5, 100),  # 7 arm 3 (declined with a cap of 2)
    (100, 101, 99.5, 100),  # 8
    (100, 101, 99.5, 100),  # 9
]
ARMS = {1, 4, 7}


def frame():
    idx = pd.date_range("2026-06-04 09:15", periods=len(ROWS), freq="5min", tz=IST)
    df = pd.DataFrame(ROWS, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000.0
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


class Scripted(Strategy):
    name = "scripted"

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        if i in ARMS and direction == 1:
            return EntrySignal(direction=1, sl=98.0, tp=110.0, tag="T")
        return None


def run_cfg(**kw):
    base = {"skip_open_minutes": 0, "entry_cutoff_min": 15 * 60, "time_exit_min": 15 * 60,
            "max_trades_per_day": 9, "min_sl_pct": 0.0, "cost_bps": 0.0}
    base.update(kw)
    return RunConfig(**base)


def engine_trades(run):
    return simulate(Ctx(frame(), "TEST"), Scripted(), None, run)


def sink_trades(run):
    sink = SimSink(SimRules.from_run_config(run, 5), interval_min=5)
    df = frame()
    for i, (ts, row) in enumerate(df.iterrows()):
        asyncio.run(sink.on_bar("TEST", ts, row.Open, row.High, row.Low, row.Close))
        asyncio.run(sink.after_bar("TEST", ts, row.Open, row.High, row.Low, row.Close))
        if i in ARMS:
            asyncio.run(sink.submit(Signal(
                strategy="BREAKOUT", direction=Direction.LONG, symbol="TEST", entry=row.Close,
                sl=98.0, tp=110.0, exchange="NSE", product="MIS", sig_id=f"TEST-{i}",
                context={"trigger": "T"},
            )))
    sink.flush()  # the replay ends: whatever is open closes at its last close, as the engine's EOD does
    return sink.trades


class TestCapOfTwo:
    def test_the_engine_takes_long_exit_long_and_declines_a_third(self):
        trades = engine_trades(run_cfg(max_entries_per_direction=2))
        assert len(trades) == 2
        assert [t.reason for t in trades] == ["SL", "SL"]

    def test_the_sim_sink_takes_long_exit_long_and_declines_a_third(self):
        assert len(sink_trades(run_cfg(max_entries_per_direction=2))) == 2

    def test_both_modes_give_the_same_trades(self):
        run = run_cfg(max_entries_per_direction=2)
        a, b = engine_trades(run), sink_trades(run)
        assert [(t.entry, t.exit, t.reason, t.direction) for t in a] == [
            (t.entry, t.exit, t.reason, t.direction) for t in b
        ]


class TestOlderRuleIsUnchanged:
    def test_one_trade_per_direction_still_blocks_the_second_long_in_both_modes(self):
        run = run_cfg(one_trade_per_direction=True)
        assert len(engine_trades(run)) == 1
        assert len(sink_trades(run)) == 1

    def test_no_per_direction_rule_allows_all_three(self):
        run = run_cfg(one_trade_per_direction=False)
        assert len(engine_trades(run)) == 3
        assert len(sink_trades(run)) == 3


class TestBreakoutAdapterCarriesTheFrozenRule:
    def test_run_overrides_cap_two_per_direction_and_four_a_day(self):
        run = RunConfig().with_(**Breakout.run_overrides)
        assert (run.max_entries_per_direction, run.max_trades_per_day) == (2, 4)
