"""Spec item 18: one bad-fill condition for the live guard, the SimSink and the backtest engine.

A fill worse than the signal by more than max_entry_slippage_r, or at or past TP1, is closed at the
fill-bar open: zero gross R, the realised cost is the loss, and the entry still counts as used.
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

IST = "Asia/Kolkata"
LONG, SHORT = 1, -1


class TestBadFillCondition:
    """bad_fill(direction, signal_ref, sl, tp1, fill, max_slippage_r); R = |signal_ref - sl| = 2."""

    @pytest.mark.parametrize(
        "direction,ref,sl,tp1,fill,expected",
        [
            (LONG, 100.0, 98.0, 104.0, 100.0, ("", 0.0)),            # filled at the signal
            (LONG, 100.0, 98.0, 104.0, 100.9, ("", 0.45)),           # inside the 0.5R cap
            (LONG, 100.0, 98.0, 104.0, 101.0, ("", 0.5)),            # exactly 0.5R is allowed
            (LONG, 100.0, 98.0, 104.0, 101.1, (tp.SLIPPAGE, 0.55)),  # just over the cap
            (LONG, 100.0, 98.0, 104.0, 99.0, ("", -0.5)),            # a better fill is never bad
            (LONG, 100.0, 98.0, 104.0, 97.9, (tp.STOP_BREACHED, -1.05)),  # gap through the stop: its own event
            (LONG, 100.0, 98.0, 104.0, 98.0, (tp.STOP_BREACHED, -1.0)),    # at the stop counts
            (LONG, 100.0, 98.0, 104.0, 98.1, ("", -0.95)),           # just above the stop is a normal fill
            (SHORT, 100.0, 102.0, 96.0, 102.3, (tp.STOP_BREACHED, -1.15)),
            (LONG, 100.0, 98.0, 100.4, 100.45, (tp.TP_OVERSHOT, 0.225)),  # past TP1 within the cap
            (LONG, 100.0, 98.0, 100.4, 100.4, (tp.TP_OVERSHOT, 0.2)),     # at TP1 counts
            (SHORT, 100.0, 102.0, 96.0, 100.0, ("", 0.0)),
            (SHORT, 100.0, 102.0, 96.0, 99.0, ("", 0.5)),
            (SHORT, 100.0, 102.0, 96.0, 98.9, (tp.SLIPPAGE, 0.55)),
            (SHORT, 100.0, 102.0, 96.0, 101.0, ("", -0.5)),
            (SHORT, 100.0, 102.0, 99.6, 99.55, (tp.TP_OVERSHOT, 0.225)),
        ],
    )
    def test_table(self, direction, ref, sl, tp1, fill, expected):
        verdict = tp.bad_fill(direction, ref, sl, tp1, fill, 0.5)
        assert (verdict.kind, round(verdict.slippage_r, 3)) == (expected[0], round(expected[1], 3))
        assert verdict.violated == bool(expected[0])

    def test_a_cap_of_zero_disables_only_the_slippage_case(self):
        assert tp.bad_fill(LONG, 100.0, 98.0, 104.0, 103.0, 0.0).violated is False
        assert tp.bad_fill(LONG, 100.0, 98.0, 104.0, 104.5, 0.0).kind == tp.TP_OVERSHOT

    def test_an_unknown_fill_is_not_a_violation(self):
        assert tp.bad_fill(LONG, 100.0, 98.0, 104.0, None, 0.5).violated is False

    def test_the_kinds_are_the_ledger_exit_types(self):
        from signal_engine import db

        assert tp.SLIPPAGE == db.EXIT_ENTRY_REJECTED_SLIPPAGE
        assert tp.STOP_BREACHED == db.EXIT_ENTRY_REJECTED_STOP_BREACHED
        assert tp.TP_OVERSHOT == db.EXIT_ENTRY_REJECTED_TP_OVERSHOT


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


class Arms(Strategy):
    name = "arms"

    def __init__(self, bars, tp_price=110.0):
        self.bars, self.tp_price = set(bars), tp_price

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        if i in self.bars and direction == 1:
            return EntrySignal(direction=1, sl=98.0, tp=self.tp_price, tag="T")
        return None


def run_cfg(**kw):
    base = {"skip_open_minutes": 0, "entry_cutoff_min": 15 * 60, "time_exit_min": 15 * 60,
            "max_trades_per_day": 9, "min_sl_pct": 0.0, "cost_bps": 16.0, "max_entries_per_direction": 1}
    base.update(kw)
    return RunConfig(**base)


def engine(rows, run, arms=(1,), tp_price=110.0):
    return simulate(Ctx(frame(rows), "TEST"), Arms(arms, tp_price), None, run)


def sink(rows, run, arms=(1,), tp_price=110.0):
    s = SimSink(SimRules.from_run_config(run, 5), interval_min=5)
    for i, (ts, row) in enumerate(frame(rows).iterrows()):
        asyncio.run(s.on_bar("TEST", ts, row.Open, row.High, row.Low, row.Close))
        asyncio.run(s.after_bar("TEST", ts, row.Open, row.High, row.Low, row.Close))
        if i in arms:
            asyncio.run(s.submit(Signal(
                strategy="BREAKOUT", direction=Direction.LONG, symbol="TEST", entry=row.Close,
                sl=98.0, tp=tp_price, exchange="NSE", product="MIS", sig_id=f"T-{i}",
                context={"trigger": "T"})))
    s.flush()
    return s.trades


# bar 1 arms at close 100 (sl 98, R = 2); bar 2 opens at 101.2 (0.6R adverse) and then trades quietly
SLIPPED = [(100, 101, 99.5, 100), (100, 101, 99.5, 100), (101.2, 101.5, 100.5, 101.0),
           (101.0, 101.5, 100.5, 101.0), (101.0, 101.5, 100.5, 101.0), (101.0, 101.5, 100.5, 101.0)]
CLEAN = [(100, 101, 99.5, 100), (100, 101, 99.5, 100), (100.2, 101.5, 99.5, 101.0),
         (101.0, 101.5, 100.5, 101.0), (101.0, 101.5, 100.5, 101.0), (101.0, 101.5, 100.5, 101.0)]


@pytest.mark.parametrize("mode", [engine, sink])
class TestModelledInEveryMode:
    def test_a_slipped_fill_closes_at_the_fill_bar_open_with_zero_gross_r(self, mode):
        trades = mode(SLIPPED, run_cfg(max_entry_slippage_r=0.5))
        assert len(trades) == 1
        t = trades[0]
        assert t.reason == "BAD_FILL_CLOSE"
        assert t.entry == t.exit == 101.2
        assert t.r_gross == 0
        assert t.r_net < 0  # the realised cost is the loss

    def test_a_clean_fill_is_unaffected(self, mode):
        trades = mode(CLEAN, run_cfg(max_entry_slippage_r=0.5))
        assert len(trades) == 1 and trades[0].reason != "BAD_FILL_CLOSE"

    def test_the_entry_counts_as_used_for_the_direction(self, mode):
        # a second long armed after the bad fill is declined with a cap of one per direction
        trades = mode(SLIPPED, run_cfg(max_entry_slippage_r=0.5), arms=(1, 3))
        assert len(trades) == 1

    def test_the_model_is_off_by_default(self, mode):
        trades = mode(SLIPPED, run_cfg())
        assert all(t.reason != "BAD_FILL_CLOSE" for t in trades)

    def test_a_tp_overshoot_is_closed_even_within_the_slippage_cap(self, mode):
        rows = [(100, 101, 99.5, 100), (100, 101, 99.5, 100), (100.45, 101.5, 100.4, 101.0),
                (101.0, 101.5, 100.5, 101.0), (101.0, 101.5, 100.5, 101.0)]
        trades = mode(rows, run_cfg(max_entry_slippage_r=0.5), tp_price=100.4)
        assert [t.reason for t in trades] == ["BAD_FILL_CLOSE"]


class TestBothModesAgree:
    def test_same_trade_for_the_same_bars(self):
        run = run_cfg(max_entry_slippage_r=0.5)
        a, b = engine(SLIPPED, run), sink(SLIPPED, run)
        assert [(t.entry, t.exit, t.reason, t.r_gross) for t in a] == [
            (t.entry, t.exit, t.reason, t.r_gross) for t in b
        ]


class TestFillThroughTheStop:
    """Owner decision 2026-10-08 (Option A): a long that gaps down through its stop is a STOP_BREACHED bad
    fill, closed at the fill-bar open and named BAD_FILL_CLOSE in every mode. Its R comes from the actual
    fill against the entry (here the entry IS the fill), not from a fixed 1R."""

    ROWS = [(100, 101, 99.5, 100), (100, 101, 99.5, 100), (97.0, 98.5, 96.5, 97.5),
            (97.5, 98.5, 96.5, 97.5), (97.5, 98.5, 96.5, 97.5)]

    @pytest.mark.parametrize("mode", [engine, sink])
    def test_it_is_a_bad_fill_close_with_the_model_on(self, mode):
        trades = mode(self.ROWS, run_cfg(max_entry_slippage_r=0.5))
        assert [t.reason for t in trades] == ["BAD_FILL_CLOSE"]
        assert trades[0].entry == trades[0].exit == 97.0
        assert trades[0].r_gross == 0

    @pytest.mark.parametrize("mode", [engine, sink])
    def test_the_entry_counts_as_used(self, mode):
        trades = mode(self.ROWS, run_cfg(max_entry_slippage_r=0.5), arms=(1, 3))
        assert len(trades) == 1

    @pytest.mark.parametrize("mode", [engine, sink])
    def test_with_the_model_off_the_older_name_remains(self, mode):
        trades = mode(self.ROWS, run_cfg())
        assert [t.reason for t in trades] == ["SL_GAP_ENTRY"]
