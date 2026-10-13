"""Previously untested engine semantics: intraday shorts, swing entry bar, custom exit fill."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx, RunConfig
from signal_engine.tests import test_exit_next_open as eno
from signal_engine.tests.test_backtest_engine import RUN, FireOnce, bars, run_one
from signal_engine.tests.test_swing_engine_semantics import Hooked, frame


# (a) intraday short side, mirrored from TestExitPriority
class TestShortSide:
    def _short(self, df, sl, tp):
        return run_one(df, FireOnce(at=1, sl=sl, tp=tp, direction=-1), RUN)

    def test_stop_hit(self):
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 112, 99, 105), (100, 101, 99, 100)])
        (t,) = self._short(df, sl=110, tp=90)
        assert t.direction == -1 and t.entry == 100
        assert t.reason == "SL" and t.exit == 110
        assert t.r_gross == pytest.approx(-1.0)

    def test_gap_through_stop_at_the_open(self):
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 101, 99, 100), (120, 125, 118, 122)])
        (t,) = self._short(df, sl=110, tp=90)
        assert t.reason == "SL_GAP" and t.exit == 120
        assert t.r_gross == pytest.approx(-2.0)

    def test_target_hit(self):
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 102, 85, 88), (100, 101, 99, 100)])
        (t,) = self._short(df, sl=110, tp=90)
        assert t.reason == "TP" and t.exit == 90
        assert t.r_gross == pytest.approx(1.0)

    def test_stop_wins_when_one_bar_spans_both_levels(self):
        df = bars([(100, 101, 99, 100), (100, 101, 99, 100),
                   (100, 120, 80, 100), (100, 101, 99, 100)])
        (t,) = self._short(df, sl=110, tp=90)
        assert t.reason == "SL" and t.exit == 110


# (b) swing engine entry-bar stop handling
def _with_low(opens, lows):
    df = frame(opens)
    df["Low"] = [float(x) for x in lows]
    return df


class TestSwingEntryBar:
    def test_same_bar_stop_on_the_entry_bar(self):
        df = _with_low([100.0] * 6, [100, 100, 90, 100, 100, 100])
        (t,) = simulate_swing(Ctx(df, "A"), Hooked({2}, stop=95.0), None, SwingRunConfig())
        assert t.entry == pytest.approx(100.0)
        assert t.reason == "SL" and t.exit == pytest.approx(95.0)
        assert pd.Timestamp(t.entry_time) == pd.Timestamp(t.exit_time) == df.index[2]

    def test_entry_bar_open_is_never_a_gap_exit(self):
        """The open is the fill, so a low far below the stop exits AT the stop, not the open."""
        df = _with_low([100.0] * 6, [100, 100, 60, 100, 100, 100])
        (t,) = simulate_swing(Ctx(df, "A"), Hooked({2}, stop=95.0), None, SwingRunConfig())
        assert t.reason == "SL" and t.exit == pytest.approx(95.0)

    def test_open_already_beyond_the_stop_is_not_entered(self):
        df = frame([100.0, 100.0, 90.0, 90.0, 90.0, 90.0])
        trades = simulate_swing(Ctx(df, "A"), Hooked({2}, stop=95.0), None, SwingRunConfig())
        assert trades == []


class TestTrailNeverWrapsAround:
    """trail() is given the last completed bar; on the very first bar there is none, and index
    -1 would silently read the LAST bar of the series."""

    def test_a_position_on_the_first_bar_gets_no_trail_call_with_a_negative_index(self):
        seen = []

        class Recording(Hooked):
            def trail(self, c, i, p, pos):
                seen.append(i)
                return None

        df = frame([100.0] * 6)
        simulate_swing(Ctx(df, "A"), Recording({0}, stop=90.0), None, SwingRunConfig())
        assert seen and min(seen) >= 0


# (c) custom_exit_fill default and fill prices
class TestCustomExitFill:
    def test_default_is_bar_close(self):
        assert RunConfig().custom_exit_fill == "bar_close"

    def test_bar_close_fills_at_close_of_judged_bar(self):
        df = eno.bars(eno.CLOSES, eno.LATE)
        run = eno.cfg(custom_exit_fill="bar_close")
        (t,) = simulate(Ctx(df, "TEST"), eno.ExitAt(2), None, run)
        assert t.exit == df["Close"].iloc[2] and t.exit_time == df.index[2]

    def test_next_open_fills_at_open_of_following_bar(self):
        df = eno.bars(eno.CLOSES, eno.LATE)
        (t,) = simulate(Ctx(df, "TEST"), eno.ExitAt(2), None, eno.cfg())
        assert t.exit == df["Open"].iloc[3] and t.exit_time == df.index[3]
        assert not math.isclose(t.exit, df["Close"].iloc[2])


# (d) metrics.clustered_t and metrics.hurdle_t, pinned to hand calculations
class TestClusteredT:
    def test_matches_hand_calculation_on_daily_means(self):
        import numpy as np

        from signal_engine.backtest.metrics import clustered_t

        r = np.array([1.0, 3.0, 2.0, 2.0, 4.0, 6.0])
        day = np.array(["d1", "d1", "d2", "d2", "d3", "d3"])
        # daily means 2, 2, 5: mean 3, sample sd sqrt(3), n = 3
        assert clustered_t(r, day) == pytest.approx(3.0 / (math.sqrt(3.0) / math.sqrt(3.0)))

    def test_counts_a_session_once_not_per_trade(self):
        import numpy as np

        from signal_engine.backtest.metrics import clustered_t

        r = np.array([1.0, 1.0, 1.0, 1.0, 2.0, 3.0])
        day = np.array(["a", "a", "a", "a", "b", "c"])
        # daily means 1, 2, 3 -> t = 2 / (1 / sqrt(3))
        assert clustered_t(r, day) == pytest.approx(2.0 * math.sqrt(3.0))

    def test_nan_when_fewer_than_three_sessions_or_zero_variance(self):
        import numpy as np

        from signal_engine.backtest.metrics import clustered_t

        assert math.isnan(clustered_t(np.array([1.0, 2.0, 3.0]), np.array(["a", "a", "b"])))
        assert math.isnan(clustered_t(np.array([1.0, 1.0, 1.0]), np.array(["a", "b", "c"])))
        assert math.isnan(clustered_t(np.array([1.0, 2.0]), np.array(["a", "b"])))


class TestHurdleT:
    @pytest.mark.parametrize("n", [1, 5, 20])
    def test_equals_sidak_quantile(self, n):
        from scipy import stats

        from signal_engine.backtest.metrics import hurdle_t

        per = 1.0 - 0.95 ** (1.0 / n)
        assert hurdle_t(n) == pytest.approx(abs(stats.t.ppf(per / 2.0, 200)))

    def test_one_trial_is_the_plain_five_percent_bar(self):
        from scipy import stats

        from signal_engine.backtest.metrics import hurdle_t

        assert hurdle_t(1) == pytest.approx(stats.t.ppf(0.975, 200))
        assert hurdle_t(0) == hurdle_t(1)

    @pytest.mark.parametrize("n", [5, 20])
    def test_sidak_is_slightly_below_bonferroni_and_grows_with_trials(self, n):
        from scipy import stats

        from signal_engine.backtest.metrics import hurdle_t

        bonferroni = abs(stats.t.ppf(0.05 / n / 2.0, 200))
        assert hurdle_t(n) < bonferroni
        assert hurdle_t(n) > bonferroni - 0.1
        assert hurdle_t(1) < hurdle_t(5) < hurdle_t(20)
