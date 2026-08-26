"""Engine semantics, pinned on synthetic bars.

These exist because every one of them is a way a backtest can quietly lie. Each test
builds the minimum number of bars needed to force one decision, so a failure names the
broken rule directly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig

IST = "Asia/Kolkata"


def bars(rows, start="09:15"):
    """rows: list of (open, high, low, close). One session of 5-minute bars."""
    idx = pd.date_range(f"2026-06-04 {start}", periods=len(rows), freq="5min", tz=IST)
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
    df["Volume"] = 1000.0
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df


class FireOnce(Strategy):
    """Arms one long at `at`, with the stop and target handed in."""

    name = "fire-once"

    def __init__(self, at, sl, tp, direction=1):
        self.at, self.sl, self.tp, self.dir = at, sl, tp, direction
        self.fired = False
        self.entries = 0

    def prepare(self, df, p):
        return df

    def reset_symbol(self, p):
        self.fired = False

    def entry(self, c, i, p, direction):
        if i != self.at or direction != self.dir or self.fired:
            return None
        return EntrySignal(direction=self.dir, sl=self.sl, tp=self.tp, tag="T")

    def on_entry(self, c, i, p, sig):
        self.fired = True
        self.entries += 1


RUN = RunConfig(skip_open_minutes=0, entry_cutoff_min=15 * 60,
                time_exit_min=15 * 60, max_trades_per_day=9, min_sl_pct=0.0)


def run_one(df, strat, run=RUN):
    return simulate(Ctx(df, "TEST"), strat, None, run)


class TestFillTiming:
    def test_signal_fills_at_the_next_bar_open(self):
        """Pine's process_orders_on_close=false, and what a live market order does."""
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (105, 106, 104, 105), (105, 106, 104, 105)])
        t = run_one(df, FireOnce(at=1, sl=90, tp=200))
        assert len(t) == 1
        assert t[0].entry == 105          # bar 2's OPEN, not bar 1's close
        assert t[0].signal_price == 100   # what the alert advertised

    def test_r_is_measured_from_the_advertised_risk(self):
        """R uses the signal-bar risk, so entry slippage shows up as lost R."""
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (105, 106, 104, 105), (110, 111, 109, 110)])
        t = run_one(df, FireOnce(at=1, sl=90, tp=110))
        assert t[0].risk == pytest.approx(10.0)   # |100 - 90|


class TestExitPriority:
    def test_stop_wins_when_one_bar_spans_both_levels(self):
        """The optimistic read is how a backtest flatters itself."""
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 120, 80, 100), (100, 101, 99, 100)])
        t = run_one(df, FireOnce(at=1, sl=90, tp=110))
        assert t[0].reason == "SL"
        assert t[0].exit == 90

    def test_gap_through_the_stop_costs_more_than_one_r(self):
        """A bar that OPENS past the stop fills there, not at the stop.

        Tight stops on 5-minute bars gap through more often than people expect, and a
        backtest that always pays exactly -1R understates the tail.
        """
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 101, 99, 100),          # entry fills here at 100
                   (80, 85, 78, 82)])            # next bar opens below the 90 stop
        t = run_one(df, FireOnce(at=1, sl=90, tp=110))
        assert t[0].entry == 100
        assert t[0].reason == "SL_GAP"
        assert t[0].exit == 80
        assert t[0].r_gross == pytest.approx(-2.0)   # not the -1.0 a naive fill assumes

    def test_filling_into_an_already_gapped_bar_exits_flat(self):
        """Entry and stop are both past: the fill is the open and the loss is ~0.

        Worth pinning because it looks wrong at a glance. The advertised risk still
        anchors R, but P&L is measured from the price actually paid.
        """
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (80, 85, 78, 82), (80, 85, 78, 82)])
        t = run_one(df, FireOnce(at=1, sl=90, tp=110))
        assert t[0].entry == 80 and t[0].exit == 80
        assert t[0].r_gross == pytest.approx(0.0)

    def test_target_taken_when_the_stop_is_untouched(self):
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 115, 98, 112), (100, 101, 99, 100)])
        t = run_one(df, FireOnce(at=1, sl=90, tp=110))
        assert t[0].reason == "TP"
        assert t[0].exit == 110


class TestNaNGuards:
    def test_a_nan_stop_is_skipped_and_does_not_consume_the_setup(self):
        """Regression: NaN passes every naive comparison.

        `risk <= 0` and `risk / close < min_sl_pct` are BOTH False when risk is NaN, so
        an unguarded engine arms a pending order with a NaN stop, lets the strategy
        consume its setup, then silently drops the fill because `NaN > 0` is False. The
        setup is destroyed and no trade is ever recorded. Found porting the EMA9
        one-off: it swallowed real day-one signals during indicator warmup.
        """
        df = bars([(100, 101, 99, 100)] * 6)
        strat = FireOnce(at=1, sl=float("nan"), tp=110)
        t = run_one(df, strat)
        assert t == []
        assert strat.entries == 0, "on_entry must not fire for a NaN stop"

    def test_stop_on_the_wrong_side_is_rejected(self):
        df = bars([(100, 101, 99, 100)] * 6)
        strat = FireOnce(at=1, sl=110, tp=120)     # stop ABOVE entry on a long
        assert run_one(df, strat) == []
        assert strat.entries == 0


class TestValidatorParity:
    def test_stop_tighter_than_min_sl_pct_is_rejected(self):
        """Mirrors signal_engine's validator: the live engine would reject this."""
        df = bars([(100, 101, 99, 100)] * 6)
        run = RUN.with_(min_sl_pct=0.01)           # 1% floor
        strat = FireOnce(at=1, sl=99.9, tp=110)    # 0.1% stop
        assert run_one(df, strat, run) == []
        assert strat.entries == 0


class TestSessionRules:
    def test_time_exit_closes_at_the_configured_minute(self):
        df = bars([(100, 101, 99, 100)] * 8, start="14:20")
        run = RUN.with_(time_exit_min=14 * 60 + 45, entry_cutoff_min=14 * 60 + 30)
        t = run_one(df, FireOnce(at=0, sl=90, tp=200), run)
        assert t[0].reason == "TIME_EXIT"
        assert t[0].exit_time.strftime("%H:%M") == "14:45"

    def test_open_skip_blocks_early_entries(self):
        df = bars([(100, 101, 99, 100)] * 6)
        run = RUN.with_(skip_open_minutes=30)
        assert run_one(df, FireOnce(at=1, sl=90, tp=110), run) == []

    def test_entry_cutoff_blocks_late_entries(self):
        df = bars([(100, 101, 99, 100)] * 6, start="11:00")
        run = RUN.with_(entry_cutoff_min=11 * 60)
        assert run_one(df, FireOnce(at=1, sl=90, tp=110), run) == []

    def test_no_signal_on_the_last_bar_of_a_session(self):
        """There is no next bar to fill into, so it must not arm."""
        df = bars([(100, 101, 99, 100)] * 3)
        assert run_one(df, FireOnce(at=2, sl=90, tp=110)) == []

    def test_direction_permission_is_honoured(self):
        df = bars([(100, 101, 99, 100)] * 6)
        run = RUN.with_(allow_longs=False)
        assert run_one(df, FireOnce(at=1, sl=90, tp=110), run) == []


class TestCosts:
    def test_cost_scales_inversely_with_stop_width(self):
        """cost-in-R = cost_pct x price / risk. The whole tuning argument rests on it."""
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 101, 99, 100), (100, 101, 99, 100)])
        run = RUN.with_(cost_bps=10.0)
        tight = run_one(df, FireOnce(at=1, sl=99, tp=200), run)[0]    # 1.0 risk
        wide = run_one(df, FireOnce(at=1, sl=90, tp=200), run)[0]     # 10.0 risk
        cost_tight = tight.r_gross - tight.r_net
        cost_wide = wide.r_gross - wide.r_net
        assert cost_tight == pytest.approx(cost_wide * 10, rel=1e-6)

    def test_zero_cost_leaves_gross_and_net_equal(self):
        df = bars([(100, 101, 99, 100)] * 5)
        t = run_one(df, FireOnce(at=1, sl=90, tp=200), RUN.with_(cost_bps=0.0))
        assert t[0].r_gross == pytest.approx(t[0].r_net)


class TestTrailRatchets:
    def test_a_trail_never_loosens_the_stop(self):
        class Loosen(FireOnce):
            def trail(self, c, i, p, pos):
                return 50.0            # far below the original stop
        df = bars([(100, 101, 99, 100)] * 4 + [(100, 101, 40, 45)])
        t = run_one(df, Loosen(at=1, sl=90, tp=200))
        assert t[0].reason == "SL"
        assert t[0].exit == 90, "the stop must not have been loosened to 50"
