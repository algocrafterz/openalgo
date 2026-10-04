"""Panel helpers for ranked strategies: weekly dates, a calendar-session window, a top-N schedule."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import panel


def sessions_between(start: date, n_days: int, holidays=()):
    out, d = [], start
    while len(out) < n_days:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def daily_frame(sess, closes):
    """A daily frame on the given dates; `closes` aligned to `sess` (NaN rows are left out)."""
    idx = pd.DatetimeIndex(pd.Timestamp(d) for d in sess).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)
    return df.dropna()


class TestWeeklyRebalanceSessions:
    def test_it_is_the_last_session_of_each_iso_week(self):
        sess = sessions_between(date(2026, 1, 5), 15)  # three full weeks, Mon 5 Jan first
        assert panel.weekly_rebalance_sessions(sess) == [date(2026, 1, 9), date(2026, 1, 16)]

    def test_a_holiday_on_friday_makes_thursday_the_rebalance_session(self):
        sess = sessions_between(date(2026, 1, 5), 14, holidays={date(2026, 1, 9)})
        assert date(2026, 1, 8) in panel.weekly_rebalance_sessions(sess)
        assert date(2026, 1, 9) not in panel.weekly_rebalance_sessions(sess)

    def test_the_final_available_session_never_rebalances(self):
        # data ends on a Friday: that Friday has no next session to execute on
        sess = sessions_between(date(2026, 1, 5), 10)
        assert sess[-1] == date(2026, 1, 16)
        assert date(2026, 1, 16) not in panel.weekly_rebalance_sessions(sess)

    def test_a_partial_final_week_does_not_rebalance(self):
        sess = sessions_between(date(2026, 1, 5), 8)  # ends Wednesday 14 Jan
        assert panel.weekly_rebalance_sessions(sess) == [date(2026, 1, 9)]

    def test_a_single_week_has_no_rebalance(self):
        assert panel.weekly_rebalance_sessions(sessions_between(date(2026, 1, 5), 5)) == []

    def test_the_week_is_the_iso_week_across_a_year_boundary(self):
        sess = sessions_between(date(2025, 12, 29), 10)  # 29 Dec 2025 is in ISO week 1 of 2026
        assert panel.weekly_rebalance_sessions(sess) == [date(2026, 1, 2)]

    def test_it_accepts_unsorted_duplicates(self):
        sess = sessions_between(date(2026, 1, 5), 15)
        assert panel.weekly_rebalance_sessions(sess[::-1] + sess) == [
            date(2026, 1, 9),
            date(2026, 1, 16),
        ]


class TestCloseAndTrailingHigh:
    def test_the_panel_is_indexed_by_the_calendar_not_by_each_symbols_rows(self):
        sess = sessions_between(date(2026, 1, 5), 6)
        a = daily_frame(sess, [1, 2, np.nan, 4, 5, 6])
        close = panel.close_panel({"A": a}, sess)
        assert list(close.index.date) == sess
        assert np.isnan(close["A"].iloc[2])

    def test_the_window_counts_calendar_sessions_not_rows(self):
        sess = sessions_between(date(2026, 1, 5), 10)
        closes = [10, 90, 11, 12, np.nan, np.nan, 13, 14, 15, 16]
        close = panel.close_panel({"A": daily_frame(sess, closes)}, sess)
        high, _ = panel.trailing_high(close, window=5)
        # window at the last session is calendar sessions 5..9: only 13, 14, 15, 16 exist.
        # A 5-row window would reach back to the 90 at session 1... it must not.
        assert high["A"].iloc[9] == 16
        assert high["A"].iloc[8] == 15
        # the 90 is inside the 5-session window until session 5 inclusive
        assert high["A"].iloc[5] == 90

    def test_a_missing_close_is_skipped_inside_the_window(self):
        sess = sessions_between(date(2026, 1, 5), 6)
        close = panel.close_panel({"A": daily_frame(sess, [5, np.nan, 3, 4, 2, 1])}, sess)
        high, _ = panel.trailing_high(close, window=4)
        assert high["A"].iloc[3] == 5  # sessions 0..3 -> 5, -, 3, 4
        assert high["A"].iloc[4] == 4  # sessions 1..4

    def test_a_symbol_needs_a_full_window_of_calendar_sessions_since_its_first_bar(self):
        sess = sessions_between(date(2026, 1, 5), 12)
        late = daily_frame(sess[6:], list(range(1, 7)))  # first bar at session 6
        close = panel.close_panel({"L": late}, sess)
        _, ok = panel.trailing_high(close, window=5)
        flags = ok["L"].tolist()
        assert not any(flags[:10])  # sessions 0..9: fewer than 5 sessions of history
        assert flags[10] and flags[11]  # first valid at session 6 + 5 - 1 = 10

    def test_the_first_valid_signal_is_exactly_the_window_th_session(self):
        sess = sessions_between(date(2026, 1, 5), 12)
        close = panel.close_panel({"A": daily_frame(sess, list(range(1, 13)))}, sess)
        _, ok = panel.trailing_high(close, window=4)
        assert ok["A"].tolist().index(True) == 3

    def test_a_dropped_row_on_a_session_leaves_it_without_a_close(self):
        sess = sessions_between(date(2026, 1, 5), 6)
        close = panel.close_panel({"A": daily_frame(sess, [1, 2, 3, np.nan, 5, 6])}, sess)
        assert np.isnan(close["A"].iloc[3])
        high, ok = panel.trailing_high(close, window=3)
        score = (close / high).where(ok)
        assert np.isnan(score["A"].iloc[3])  # not rankable on a session it has no bar

    def test_a_row_dropped_for_eligibility_does_not_shift_the_window(self):
        sess = sessions_between(date(2026, 1, 5), 8)
        full = panel.close_panel({"A": daily_frame(sess, [1, 2, 3, 4, 5, 6, 7, 8])}, sess)
        gap = panel.close_panel({"A": daily_frame(sess, [1, 2, np.nan, 4, 5, 6, 7, 8])}, sess)
        hf, _ = panel.trailing_high(full, window=4)
        hg, _ = panel.trailing_high(gap, window=4)
        assert hf["A"].iloc[7] == hg["A"].iloc[7] == 8
        # the window still ends 4 calendar sessions back: sessions 4..7
        assert hg["A"].iloc[5] == 6


class TestTopNSchedule:
    def score(self, values: dict, d=date(2026, 1, 9)):
        return pd.DataFrame(values, index=pd.DatetimeIndex([pd.Timestamp(d)]))

    def test_only_names_at_or_above_the_threshold_qualify(self):
        s = self.score({"A": 1.0, "B": 0.95, "C": 0.9499})
        got = panel.top_n_schedule(s, [date(2026, 1, 9)], n=20, min_score=0.95)
        assert set(got[date(2026, 1, 9)]) == {"A", "B"}

    def test_it_holds_the_n_highest_scores(self):
        s = self.score({f"S{i}": 1.0 - i * 0.001 for i in range(30)})
        got = panel.top_n_schedule(s, [date(2026, 1, 9)], n=20, min_score=0.9)
        assert got[date(2026, 1, 9)] == [f"S{i}" for i in range(20)]

    def test_fewer_than_n_qualifiers_means_fewer_positions_not_a_fill(self):
        s = self.score({"A": 1.0, "B": 0.96, "C": 0.5, "D": np.nan})
        got = panel.top_n_schedule(s, [date(2026, 1, 9)], n=20, min_score=0.95)
        assert got[date(2026, 1, 9)] == ["A", "B"]

    def test_a_nan_score_is_not_rankable(self):
        s = self.score({"A": np.nan, "B": 0.99})
        got = panel.top_n_schedule(s, [date(2026, 1, 9)], n=5, min_score=0.0)
        assert got[date(2026, 1, 9)] == ["B"]

    def test_a_date_with_no_qualifier_has_an_empty_set(self):
        s = self.score({"A": 0.5, "B": 0.4})
        assert panel.top_n_schedule(s, [date(2026, 1, 9)], n=20, min_score=0.95) == {
            date(2026, 1, 9): []
        }

    def test_ties_are_broken_by_a_hash_of_symbol_and_date_not_alphabetically(self):
        names = [f"S{i:02d}" for i in range(40)]
        d = date(2026, 1, 9)
        s = self.score(dict.fromkeys(names, 1.0), d)
        got = panel.top_n_schedule(s, [d], n=20, min_score=0.95)[d]
        expected = sorted(
            names, key=lambda n: hashlib.sha1(f"{n}|{d.isoformat()}".encode()).hexdigest()
        )[:20]
        assert got == expected
        assert got != names[:20]

    def test_the_tie_break_is_stable_whatever_the_column_order(self):
        names = [f"S{i:02d}" for i in range(40)]
        d = date(2026, 1, 9)
        a = panel.top_n_schedule(self.score(dict.fromkeys(names, 1.0), d), [d], 20, 0.95)
        b = panel.top_n_schedule(self.score(dict.fromkeys(reversed(names), 1.0), d), [d], 20, 0.95)
        assert a == b

    def test_each_rebalance_date_is_ranked_independently(self):
        idx = pd.DatetimeIndex([pd.Timestamp(date(2026, 1, 9)), pd.Timestamp(date(2026, 1, 16))])
        s = pd.DataFrame({"A": [1.0, 0.5], "B": [0.5, 1.0]}, index=idx)
        got = panel.top_n_schedule(s, [date(2026, 1, 9), date(2026, 1, 16)], 1, 0.95)
        assert got == {date(2026, 1, 9): ["A"], date(2026, 1, 16): ["B"]}


class TestAttachSchedule:
    @pytest.fixture
    def setup(self):
        sess = sessions_between(date(2026, 1, 5), 10)
        rebalances = [sess[2], sess[7]]
        schedule = {sess[2]: ["A"], sess[7]: ["B"]}
        return sess, rebalances, schedule

    def test_the_session_after_a_rebalance_carries_that_rebalances_decision(self, setup):
        sess, rebalances, schedule = setup
        out = panel.attach_schedule(
            {"A": daily_frame(sess, list(range(1, 11)))}, rebalances, schedule, sess
        )["A"]
        assert out["rebalance_prev"].tolist() == [
            False,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
            False,
        ]
        assert out["target_prev"].tolist()[3] is True or out["target_prev"].iloc[3]
        assert not out["target_prev"].iloc[8]  # A is not in the second schedule

    def test_the_latest_decision_holds_until_the_next_rebalance(self, setup):
        sess, rebalances, schedule = setup
        out = panel.attach_schedule(
            {"A": daily_frame(sess, list(range(1, 11)))}, rebalances, schedule, sess
        )["A"]
        assert out["target_latest"].tolist() == [False] * 3 + [True] * 5 + [False] * 2

    def test_a_symbol_with_no_bar_on_the_execution_session_does_not_enter_late(self, setup):
        sess, rebalances, schedule = setup
        closes = list(range(1, 11))
        closes[3] = np.nan  # no bar on the session after the first rebalance
        out = panel.attach_schedule({"A": daily_frame(sess, closes)}, rebalances, schedule, sess)[
            "A"
        ]
        assert not out["rebalance_prev"].iloc[3]  # row 3 is session 4 now: not the next session
        # the decision still stands for a later exit check
        assert out["target_latest"].iloc[3]

    def test_a_decision_is_not_visible_on_the_rebalance_session_itself(self, setup):
        sess, rebalances, schedule = setup
        out = panel.attach_schedule(
            {"A": daily_frame(sess, list(range(1, 11)))}, rebalances, schedule, sess
        )["A"]
        assert not out["target_latest"].iloc[2]
        assert not out["rebalance_prev"].iloc[2]

    def test_the_input_frames_are_not_modified(self, setup):
        sess, rebalances, schedule = setup
        frame = daily_frame(sess, list(range(1, 11)))
        before = frame.copy()
        panel.attach_schedule({"A": frame}, rebalances, schedule, sess)
        pd.testing.assert_frame_equal(frame, before)


class TestTwoFiftyTwoSessionWindow:
    """The real window length: 252 calendar sessions, counted from the symbol's first bar."""

    SESS = sessions_between(date(2024, 1, 1), 320)

    def close(self, frames):
        return panel.close_panel(frames, self.SESS)

    def test_the_first_valid_signal_is_the_252nd_session_of_history(self):
        a = daily_frame(self.SESS, list(np.linspace(100, 200, 320)))
        _, ok = panel.trailing_high(self.close({"A": a}), window=252)
        flags = ok["A"].tolist()
        assert flags.index(True) == 251
        assert not any(flags[:251])

    def test_insufficient_history_is_never_rankable(self):
        late = daily_frame(self.SESS[100:], list(np.linspace(100, 150, 220)))
        _, ok = panel.trailing_high(self.close({"L": late}), window=252)
        assert not ok["L"].any()  # 220 sessions of history is not 252

    def test_history_counts_from_the_first_bar_the_engine_sees(self):
        # a symbol whose first 60 sessions were removed as ineligible starts at session 60
        trimmed = daily_frame(self.SESS[60:], list(np.linspace(100, 150, 260)))
        _, ok = panel.trailing_high(self.close({"T": trimmed}), window=252)
        assert ok["T"].tolist().index(True) == 60 + 251

    def test_ineligible_rows_inside_the_window_do_not_stretch_it(self):
        closes = list(np.linspace(100, 200, 320))
        closes[200:205] = [np.nan] * 5  # five sessions dropped inside the window
        a = daily_frame(self.SESS, closes)
        high, ok = panel.trailing_high(self.close({"A": a}), window=252)
        t = 300
        window = [c for c in closes[t - 251 : t + 1] if not np.isnan(c)]
        assert high["A"].iloc[t] == pytest.approx(max(window))
        # 252 rows would reach one session further back than the window does
        rows = [c for c in closes[: t + 1] if not np.isnan(c)][-252:]
        assert len(rows) == 252 and len(window) == 247

    def test_a_row_dropped_on_the_signal_session_is_not_rankable_that_day(self):
        closes = list(np.linspace(100, 200, 320))
        closes[300] = np.nan
        c = self.close({"A": daily_frame(self.SESS, closes)})
        high, ok = panel.trailing_high(c, window=252)
        score = (c / high).where(ok & c.notna())
        assert np.isnan(score["A"].iloc[300]) and not np.isnan(score["A"].iloc[301])
