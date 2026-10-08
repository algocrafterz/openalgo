"""Custom exits that fill at the NEXT bar open (RunConfig.custom_exit_fill = "next_open").

The default "bar_close" mode keeps the older rule: custom_exit closes at the signal bar's close.
In "next_open" mode the exit is judged on the completed bar and the order fills at the next bar's
open, which mirrors how entries already work (signal at close, fill at next open). The clock exit
(TIME_EXIT, EOD) keeps its own rule and always wins over a custom exit on the same bar.

A strategy with no stop (EntrySignal.sl is None) also runs in the intraday engine: R is undefined
and the result is carried as gross_ret and net_ret.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig

IST = "Asia/Kolkata"
TE = 14 * 60 + 45
LATE = ["14:25", "14:30", "14:35", "14:40", "14:45", "14:50"]
EARLY = ["09:30", "09:35", "09:40", "09:45", "09:50"]


def bars(closes, times, mirror=False):
    """Bars whose open is the previous close + 0.4, so an open fill and a close fill never tie."""
    rows = []
    for i, cl in enumerate(closes):
        op = cl if i == 0 else closes[i - 1] + 0.4
        rows.append([op, max(op, cl) + 0.5, min(op, cl) - 0.5, cl])
    if mirror:
        rows = [[200 - r[0], 200 - r[2], 200 - r[1], 200 - r[3]] for r in rows]
    idx = pd.DatetimeIndex([pd.Timestamp(f"2026-06-04 {t}", tz=IST) for t in times])
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000.0
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


class ExitAt(Strategy):
    """Arms a long (or short) at bar 0 and asks for a custom exit on bar `exit_bar`."""

    name = "exit-at"

    def __init__(self, exit_bar, direction=1, sl=None, seen=None):
        self.exit_bar, self.dir, self.sl, self.seen = exit_bar, direction, sl, seen

    def prepare(self, df, p):
        return df

    def entry(self, c, i, p, direction):
        if i == 0 and direction == self.dir:
            return EntrySignal(direction=direction, sl=self.sl, tp=float("nan"), tag="T")
        return None

    def custom_exit(self, c, i, p, pos):
        if self.seen is not None:
            self.seen.append(i)
        return "ZERO" if i == self.exit_bar else None


def cfg(**kw):
    base = {
        "skip_open_minutes": 0, "entry_cutoff_min": 15 * 60, "time_exit_min": TE,
        "max_trades_per_day": 9, "min_sl_pct": 0.0, "cost_bps": 0.0,
        "allow_shorts": True, "custom_exit_fill": "next_open",
    }
    base.update(kw)
    return RunConfig(**base)


def run(closes, times, exit_bar, direction=1, sl=None, mirror=False, **kw):
    df = bars(closes, times, mirror=mirror)
    trades = simulate(Ctx(df, "TEST"), ExitAt(exit_bar, direction, sl), None, cfg(**kw))
    return df, trades


CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]


class TestNextOpenFill:
    def test_exit_fills_at_next_bar_open_not_signal_close(self):
        df, (t,) = run(CLOSES, LATE, exit_bar=2)
        assert t.reason == "ZERO"
        assert t.exit == df["Open"].iloc[3]
        assert t.exit != df["Close"].iloc[2]
        assert t.exit_time == df.index[3]
        assert t.exit_at_open is True

    def test_entry_is_next_open_after_signal(self):
        df, (t,) = run(CLOSES, LATE, exit_bar=2)
        assert t.entry == df["Open"].iloc[1]
        assert t.entry_time == df.index[1]

    def test_default_mode_still_exits_at_signal_close(self):
        df, (t,) = run(CLOSES, LATE, exit_bar=2, custom_exit_fill="bar_close")
        assert t.exit == df["Close"].iloc[2]
        assert t.exit_time == df.index[2]
        assert t.exit_at_open is False

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            run(CLOSES, LATE, exit_bar=2, custom_exit_fill="sometime")

    def test_pending_exit_fills_before_the_stop_is_checked_on_that_bar(self):
        df = bars(CLOSES, LATE)
        df.iloc[3, df.columns.get_loc("Low")] = 90.0  # fill bar trades far through a 98 stop
        (t,) = simulate(Ctx(df, "TEST"), ExitAt(2, 1, sl=98.0), None, cfg())
        assert t.reason == "ZERO"
        assert t.exit == df["Open"].iloc[3]


class TestClockInteraction:
    def test_signal_on_bar_before_time_exit_fills_at_the_time_exit_bar_open(self):
        df, (t,) = run(CLOSES, LATE, exit_bar=3)  # 14:40 close, fills 14:45 open
        assert t.reason == "ZERO"
        assert t.exit == df["Open"].iloc[4]
        assert t.exit_time == df.index[4]

    def test_signal_on_the_time_exit_bar_is_the_clock_exit_at_close(self):
        df, (t,) = run(CLOSES, LATE, exit_bar=4)  # 14:45 bar itself
        assert t.reason == "TIME_EXIT"
        assert t.exit == df["Close"].iloc[4]

    def test_signal_on_last_bar_of_session_is_eod_not_a_custom_fill(self):
        df, (t,) = run(CLOSES[:5], EARLY, exit_bar=4)
        assert t.reason == "EOD"
        assert t.exit == df["Close"].iloc[4]
        assert t.exit_at_open is False

    def test_signal_on_second_last_bar_fills_at_last_bar_open(self):
        df, (t,) = run(CLOSES[:5], EARLY, exit_bar=3)
        assert t.reason == "ZERO"
        assert t.exit == df["Open"].iloc[4]

    def test_custom_exit_keeps_its_name_when_the_clock_exit_shares_the_open(self):
        # custom signal at 14:40 close and the clock exit at the 14:45 open: both fill at that
        # open. The custom exit was ordered first, so it keeps the name.
        df, (t,) = run(CLOSES, LATE, exit_bar=3, time_exit_at_open=True)
        assert t.reason == "ZERO"
        assert t.exit == df["Open"].iloc[4]


class TestSymmetryAndLookahead:
    def test_long_and_short_are_mirror_images(self):
        df_l, (tl,) = run(CLOSES, LATE, exit_bar=2)
        df_s, (ts,) = run(CLOSES, LATE, exit_bar=2, direction=-1, mirror=True)
        assert ts.direction == -1 and tl.direction == 1
        assert ts.reason == tl.reason == "ZERO"
        assert ts.exit_time == tl.exit_time
        assert (ts.entry - ts.exit) == pytest.approx(tl.exit - tl.entry)

    def test_exit_decision_does_not_depend_on_later_bars(self):
        a = bars(CLOSES, LATE)
        b = a.copy()
        b.iloc[4:, b.columns.get_indexer(["High", "Low", "Close"])] = 500.0  # change only the future
        (ta,) = simulate(Ctx(a, "TEST"), ExitAt(2), None, cfg())
        (tb,) = simulate(Ctx(b, "TEST"), ExitAt(2), None, cfg())
        assert ta.exit_time == tb.exit_time and ta.reason == tb.reason
        assert ta.exit == tb.exit == a["Open"].iloc[3]

    def test_custom_exit_is_not_asked_again_once_flat(self):
        seen = []
        df = bars(CLOSES, LATE)
        simulate(Ctx(df, "TEST"), ExitAt(2, seen=seen), None, cfg())
        assert seen == [1, 2]  # filled at bar 1 open, asked on 1 and 2, flat from bar 3


class TestNoStopTrade:
    def test_no_stop_trade_carries_return_not_r(self):
        df, (t,) = run(CLOSES, LATE, exit_bar=2)
        assert np.isnan(t.sl) and np.isnan(t.risk)
        assert not t.r_defined
        expected = (df["Open"].iloc[3] - df["Open"].iloc[1]) / df["Open"].iloc[1]
        assert t.gross_ret == pytest.approx(expected)
        assert t.net_ret == pytest.approx(expected)  # cost_bps is 0 in this fixture

    def test_no_stop_net_return_deducts_cost(self):
        _, (t,) = run(CLOSES, LATE, exit_bar=2, cost_bps=10.0)
        assert t.net_ret == pytest.approx(t.gross_ret - 0.001)

    def test_no_stop_position_is_never_stopped_out(self):
        df = bars(CLOSES, LATE)
        df.iloc[2, df.columns.get_loc("Low")] = 1.0  # a crash bar cannot hit a stop that is not there
        (t,) = simulate(Ctx(df, "TEST"), ExitAt(3), None, cfg())
        assert t.reason == "ZERO"
