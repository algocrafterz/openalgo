"""Engine 1.6 missing-bar execution guard (hand-built 5-minute IST frames).

A pending order armed on signal bar s is due at the bar stamped times[s] + (1 + delay) * bar_min.
If the bar actually sitting at that index carries another stamp, the order is dropped
(ENTRY_SKIPPED_MISSING_BAR) instead of filling across the hole. A position that crosses a hole
is flagged Trade.data_gap but is still evaluated on the next available bar.
"""

from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from signal_engine.backtest import ledgers
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig, Trade

IST = "Asia/Kolkata"
DAY1, DAY2 = "2026-06-04", "2026-06-05"

RUN = RunConfig(
    skip_open_minutes=0,
    entry_cutoff_min=15 * 60,
    time_exit_min=15 * 60 + 15,
    time_exit_at_open=True,
    mis_squareoff_min=15 * 60 + 25,
    squareoff_margin_min=5,
    max_trades_per_day=9,
    one_trade_per_direction=False,
    min_sl_pct=0.0,
    cost_bps=0.0,
)

FLAT100 = (100.0, 101.0, 99.0, 100.0)
FLAT110 = (110.0, 111.0, 109.0, 110.0)


def frame(rows, times, day=DAY1):
    """rows: (o, h, l, c); times: 'HH:MM' strings. Session columns are built like the loader."""
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} {t}", tz=IST) for t in times])
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx, dtype="float64")
    df["Volume"] = 1000.0
    d = pd.Series(df.index.date, index=df.index)
    df["day"] = d
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (d != d.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - d.map(df.groupby(d)["mins"].min())
    return df


def contiguous(n, start="09:15", day=DAY1):
    idx = pd.date_range(f"{day} {start}", periods=n, freq="5min", tz=IST)
    return [t.strftime("%H:%M") for t in idx]


class FireOnce(Strategy):
    name = "fire-once"

    def __init__(self, at, sl, tp, direction=1):
        self.at, self.sl, self.tp, self.dir = at, sl, tp, direction
        self.fired = False
        self.events = []

    def prepare(self, df, p):
        return df

    def reset_symbol(self, p):
        self.fired = False

    def entry(self, c, i, p, d):
        if i != self.at or d != self.dir or self.fired:
            return None
        return EntrySignal(direction=self.dir, sl=self.sl, tp=self.tp, tag="T")

    def on_entry(self, c, i, p, sig):
        self.fired = True

    def on_fill(self, c, i, p, pos):
        self.events.append(("fill", i))

    def on_fill_skipped(self, c, i, p, sig):
        self.events.append(("skipped", i))


def go(df, strat, run=RUN, rec=None, **kw):
    return simulate(Ctx(df, "TEST"), strat, None, run, rec, **kw)


class TestReasonCodeContract:
    def test_new_code_is_appended_and_old_indices_are_unchanged(self):
        # SESSION_AFTER_GAP (session-coverage safeguard) was appended after it
        assert ledgers.REASON_CODES[-2] == "ENTRY_SKIPPED_MISSING_BAR"
        assert ledgers.CODE_INDEX["WARMUP"] == 17
        assert ledgers.CODE_INDEX["ENTRY_SKIPPED_BEYOND_LEVEL"] == 18
        assert ledgers.CODE_INDEX["ENTRY_SKIPPED_MISSING_BAR"] == 19
        assert len(ledgers.REASON_CODES) == 21


class TestEntryInvalidation:
    def test_signal_at_0930_with_next_bar_at_1000_does_not_fill(self):
        times = ["09:15", "09:20", "09:25", "09:30", "10:00", "10:05", "10:10"]
        df = frame([FLAT100] * 4 + [FLAT110] * 3, times)
        s = FireOnce(at=3, sl=90.0, tp=130.0)
        rec = LedgerRecorder()
        assert go(df, s, rec=rec) == []
        assert s.events == [("skipped", 4)]
        f = rec.to_frames()
        exc = f["exclusions"]
        assert set(exc.loc[exc["scope"] == "order", "reason_code"]) == {"ENTRY_SKIPPED_MISSING_BAR"}
        assert f["orders"].iloc[0]["status"] == "DROPPED"
        assert rec.reconcile() == []

    def test_contiguous_bars_still_fill(self):
        df = frame([FLAT100] * 4 + [FLAT110] * 3, contiguous(7))
        s = FireOnce(at=3, sl=90.0, tp=130.0)
        t = go(df, s)
        assert len(t) == 1 and t[0].entry == 110.0
        assert s.events == [("fill", 4)]

    def test_skipped_order_uses_no_trade_slot(self):
        class Twice(FireOnce):
            def entry(self, c, i, p, d):
                if d == 1 and i in (3, 6):
                    return EntrySignal(direction=1, sl=90.0, tp=130.0, tag="T")
                return None

        times = ["09:15", "09:20", "09:25", "09:30", "10:00", "10:05", "10:10", "10:15"]
        df = frame([FLAT100] * 4 + [FLAT110] * 4, times)
        t = go(df, Twice(at=0, sl=0, tp=0), RUN.with_(max_trades_per_day=1))
        assert len(t) == 1 and t[0].entry_time == df.index[7]

    def test_hole_with_entry_delay_invalidates_the_delayed_fill(self):
        # delay 1: signal 09:30 is due at 09:40. Bars 09:35 and 09:40 present, so it fills.
        df = frame([FLAT100] * 4 + [FLAT110] * 3, contiguous(7))
        assert len(go(df, FireOnce(at=3, sl=90.0, tp=130.0), entry_delay_bars=1)) == 1
        # 09:35 present, 09:40 missing (next stamp 09:45): the due bar is not the bar at pend_due
        times = ["09:15", "09:20", "09:25", "09:30", "09:35", "09:45", "09:50"]
        df = frame([FLAT100] * 5 + [FLAT110] * 2, times)
        s = FireOnce(at=3, sl=90.0, tp=130.0)
        assert go(df, s, entry_delay_bars=1) == []
        assert s.events == [("skipped", 5)]

    def test_hole_before_the_signal_bar_does_not_block_the_entry(self):
        times = ["09:15", "09:35", "09:40", "09:45", "09:50"]
        df = frame([FLAT100] * 5, times)
        t = go(df, FireOnce(at=2, sl=90.0, tp=130.0))
        assert len(t) == 1 and t[0].entry_time == df.index[3]

    def test_new_session_hole_is_not_a_missing_bar(self):
        # day 1 ends at 15:25, day 2 starts at 09:15: the cross-session step is not a hole
        d1 = frame([FLAT100] * 3, contiguous(3, "15:15"))
        d2 = frame([FLAT100] * 4, contiguous(4), day=DAY2)
        df = pd.concat([d1, d2])
        d = pd.Series(df.index.date, index=df.index)
        df["new_session"] = (d != d.shift(1)).to_numpy()
        t = go(df, FireOnce(at=4, sl=90.0, tp=130.0))
        assert len(t) == 1


class TestInTradeGap:
    def test_exit_on_the_next_available_bar_is_flagged_data_gap(self):
        # fills 09:20 @100; 09:25 and 09:30 flat; hole; 10:00 reaches the target 105
        times = ["09:15", "09:20", "09:25", "09:30", "10:00", "10:05"]
        rows = [FLAT100] * 4 + [(100.0, 106.0, 100.0, 105.0), FLAT100]
        df = frame(rows, times)
        t = go(df, FireOnce(at=0, sl=90.0, tp=105.0))
        assert len(t) == 1
        assert t[0].reason == "TP" and t[0].exit == 105.0
        assert t[0].exit_time == df.index[4]
        assert t[0].data_gap is True

    def test_stop_gap_through_after_a_hole_fills_at_that_bars_open(self):
        times = ["09:15", "09:20", "09:25", "10:00", "10:05"]
        rows = [FLAT100] * 3 + [(90.0, 91.0, 89.0, 90.0), FLAT100]
        df = frame(rows, times)
        t = go(df, FireOnce(at=0, sl=95.0, tp=130.0))
        assert t[0].reason == "SL_GAP" and t[0].exit == 90.0
        assert t[0].data_gap is True

    def test_contiguous_trade_is_not_flagged(self):
        rows = [FLAT100] * 3 + [(100.0, 106.0, 100.0, 105.0), FLAT100]
        df = frame(rows, contiguous(5))
        t = go(df, FireOnce(at=0, sl=90.0, tp=105.0))
        assert t[0].data_gap is False

    def test_hole_before_entry_is_not_an_in_trade_gap(self):
        times = ["09:15", "09:35", "09:40", "09:45", "09:50"]
        rows = [FLAT100] * 4 + [(100.0, 106.0, 100.0, 105.0)]
        df = frame(rows, times)
        t = go(df, FireOnce(at=1, sl=90.0, tp=105.0))
        assert t[0].data_gap is False


class TestTradeFields:
    def test_new_fields_are_last_with_false_defaults(self):
        names = [f.name for f in dataclasses.fields(Trade)]
        assert names[-2:] == ["exit_ambiguous", "data_gap"]
        t = Trade(
            symbol="X",
            day=None,
            direction=1,
            tag="",
            entry_time=None,
            entry=1.0,
            signal_price=1.0,
            sl=0.5,
            tp=2.0,
            risk=0.5,
        )
        assert t.exit_ambiguous is False and t.data_gap is False


@pytest.mark.parametrize("delay", [0, 1])
def test_unrecorded_run_has_the_same_result_as_a_recorded_one(delay):
    times = ["09:15", "09:20", "09:25", "09:30", "10:00", "10:05", "10:10"]
    df = frame([FLAT100] * 4 + [FLAT110] * 3, times)
    a = go(df, FireOnce(at=3, sl=90.0, tp=130.0), entry_delay_bars=delay)
    b = go(df, FireOnce(at=3, sl=90.0, tp=130.0), rec=LedgerRecorder(), entry_delay_bars=delay)
    assert a == b == []


class TestRecordedRun:
    """A recorded run counts the dropped entries from the ledgers and flags the trades."""

    def test_skipped_entries_and_gap_trades_reach_the_execution(self, monkeypatch):
        from signal_engine.backtest import indicators as ind
        from signal_engine.tests.published_fixtures import frames_resolution
        from signal_engine.tests.test_backtest_lookahead_guard import _registered, _sessions

        class Fixed(Strategy):
            name = "fixed"
            run_overrides = {"skip_open_minutes": 0, "entry_cutoff_min": 15 * 60}

            def prepare(self, df, p):
                return ind.add_session_columns(df)

            def entry(self, c, i, p, d):
                if d == 1 and c["from_open"][i] == 50:  # 09:15 + 50 minutes
                    cl = c["Close"][i]
                    return EntrySignal(1, sl=cl - 2.0, tp=cl + 2.0, tag="fixed")
                return None

        frames = _sessions()
        days = sorted({t.date() for t in frames.index})
        hole_day = days[2]
        # drop the bar right after the signal bar (10:05 is bar 10; 10:10 is the due bar)
        drop = pd.Timestamp(f"{hole_day} 10:10", tz=IST)
        assert drop in frames.index
        frames = frames.drop(drop)
        rr = _registered(monkeypatch, Fixed)
        ex = rr.execute_backtest("k", dataset=frames_resolution(intraday={"AAA": frames}))
        assert ex.entries_skipped_missing_bar == 1
        exc = ex.ledgers["exclusions"]
        assert exc.loc[exc["reason_code"] == "ENTRY_SKIPPED_MISSING_BAR", "count"].sum() == 1
        assert len(ex.trades) == len(days) - 1  # every other session still trades once
        assert ex.ledger_problems == []


class TestBarIntervalIsReadFromTheData:
    """The guard must read the real interval, not the median step (review finding B1)."""

    def test_sparse_one_minute_data_judges_each_fill_on_its_own_stamp(self):
        # Steps are 2,2,2,1,2,2,1 minutes: the median step is 2, the real interval is 1.
        times = ["10:00", "10:02", "10:04", "10:06", "10:07", "10:09", "10:11", "10:12"]
        df = frame([FLAT100] * 4 + [FLAT110] * 4, times)
        # signal 10:06, next bar 10:07 is exactly one minute later: it fills
        t = go(df, FireOnce(at=3, sl=90.0, tp=130.0))
        assert len(t) == 1 and t[0].entry_time == df.index[4]
        # signal 10:04, next bar 10:06: 10:05 is missing, so it must not fill
        s = FireOnce(at=2, sl=90.0, tp=130.0)
        assert go(df, s) == []
        assert s.events == [("skipped", 3)]

    def test_bars_longer_than_an_hour_still_fill_on_the_next_bar(self):
        times = ["09:15", "10:30", "11:45", "13:00", "14:15"]
        df = frame([FLAT100] * 2 + [FLAT110] * 3, times)
        t = go(df, FireOnce(at=1, sl=90.0, tp=130.0))
        assert len(t) == 1 and t[0].entry_time == df.index[2] and t[0].entry == 110.0
        # a bar missing between signal and fill (13:00 absent, next is 14:15) is still caught
        times = ["09:15", "10:30", "11:45", "14:15", "15:30"]
        df = frame([FLAT100] * 3 + [FLAT110] * 2, times)
        assert go(df, FireOnce(at=2, sl=90.0, tp=130.0)) == []
