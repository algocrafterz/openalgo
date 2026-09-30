"""Previous-session value area, ported from klAddBar / klFinalize / klProfile (pine 2266-2360).

Hand-worked cases use tick=1.0 and rows_per_tick=1 so one row is one price unit; the row
price is (k + 0.5) * step, exactly as the Pine reports it.
"""

import numpy as np
import pandas as pd

from signal_engine.features.value_area import (
    finalize_profile,
    histogram,
    prev_session_value_area,
    session_value_areas,
)

IST = "Asia/Kolkata"


def bar_hist(bars, tick=1.0, rows=1):
    lo = np.array([b[0] for b in bars], dtype=float)
    hi = np.array([b[1] for b in bars], dtype=float)
    vol = np.array([b[2] for b in bars], dtype=float)
    return histogram(lo, hi, vol, tick, rows)


class TestHistogram:
    def test_single_row_bar_puts_all_volume_in_that_row(self):
        assert bar_hist([(10.2, 10.8, 100)]) == {10: 100.0}

    def test_multi_row_bar_spreads_volume_evenly(self):
        assert bar_hist([(10.0, 12.9, 300)]) == {10: 100.0, 11: 100.0, 12: 100.0}

    def test_zero_volume_bar_is_ignored(self):
        assert bar_hist([(10.0, 12.0, 0)]) == {}

    def test_pathological_span_lands_on_midpoint_row(self):
        assert bar_hist([(0.0, 5000.0, 90)]) == {2500: 90.0}

    def test_bars_accumulate(self):
        assert bar_hist([(10.5, 10.5, 10), (10.6, 10.7, 15)]) == {10: 25.0}

    def test_rows_per_tick_groups_ticks(self):
        # tick 0.05, 5 ticks per row -> step 0.25; 10.00..10.20 is one row (index 40)
        h = histogram(np.array([10.00]), np.array([10.20]), np.array([50.0]), 0.05, 5)
        assert h == {40: 50.0}

    def test_tick_grid_is_exact_not_float_fragile(self):
        # 100.25 sits exactly on a row boundary (step 0.25); it must land in row 401
        h = histogram(np.array([100.25]), np.array([100.25]), np.array([10.0]), 0.05, 5)
        assert h == {401: 10.0}


class TestFinalizeProfile:
    def test_single_row_profile(self):
        assert finalize_profile({10: 100.0}, 0.70, 1.0) == (10.5, 10.5, 10.5)

    def test_poc_tie_takes_first_key(self):
        vah, poc, val = finalize_profile({10: 100.0, 11: 100.0, 12: 100.0}, 0.70, 1.0)
        assert poc == 10.5
        assert (val, vah) == (10.5, 12.5)  # only one side available, grows up to 70%+

    def test_grows_toward_the_fatter_neighbour(self):
        vah, poc, val = finalize_profile({10: 10.0, 11: 50.0, 12: 30.0, 13: 10.0}, 0.70, 1.0)
        assert poc == 11.5
        assert (val, vah) == (11.5, 12.5)  # 50 + 30 = 80 >= 70, never touches row 10

    def test_tie_between_neighbours_goes_up(self):
        vah, poc, val = finalize_profile({9: 20.0, 10: 40.0, 11: 20.0, 12: 20.0}, 0.70, 1.0)
        assert poc == 10.5
        assert (val, vah) == (10.5, 12.5)

    def test_gap_rows_count_as_zero_volume(self):
        vah, poc, val = finalize_profile({10: 60.0, 13: 40.0}, 0.70, 1.0)
        assert poc == 10.5
        assert vah == 13.5  # walks up through empty rows 11, 12 to reach 70%

    def test_empty_profile_is_nan(self):
        vah, poc, val = finalize_profile({}, 0.70, 1.0)
        assert np.isnan(vah) and np.isnan(poc) and np.isnan(val)


def minute_session(day, prices_vol):
    """1m bars from 09:15 with (low, high, volume) tuples."""
    idx = pd.date_range(f"{day} 09:15", periods=len(prices_vol), freq="1min", tz=IST)
    lo = [p[0] for p in prices_vol]
    hi = [p[1] for p in prices_vol]
    mid = [(a + b) / 2 for a, b in zip(lo, hi, strict=True)]
    return pd.DataFrame(
        {
            "Open": mid,
            "High": hi,
            "Low": lo,
            "Close": mid,
            "Volume": [float(p[2]) for p in prices_vol],
        },
        index=idx,
    )


def three_sessions():
    return pd.concat(
        [
            minute_session("2026-09-28", [(10.2, 10.8, 100), (11.1, 11.9, 300)]),
            minute_session("2026-09-29", [(20.1, 20.9, 50), (20.2, 20.8, 50)]),
            minute_session("2026-09-30", [(30.1, 30.9, 10)]),
        ]
    )


def day_of(index, iso):
    return pd.Series(index.date, index=index) == pd.Timestamp(iso).date()


class TestSessionValueAreas:
    def test_one_row_per_session(self):
        df = pd.concat(
            [
                minute_session("2026-09-28", [(10.2, 10.8, 100), (11.1, 11.9, 300)]),
                minute_session("2026-09-29", [(20.1, 20.9, 50)]),
            ]
        )
        out = session_value_areas(df, tick=1.0, rows_per_tick=1)
        assert list(out.index) == [
            pd.Timestamp("2026-09-28").date(),
            pd.Timestamp("2026-09-29").date(),
        ]
        assert out.loc[pd.Timestamp("2026-09-28").date(), "poc"] == 11.5  # 300 in row 11
        assert out.loc[pd.Timestamp("2026-09-29").date(), "poc"] == 20.5


def session_with_auction_print():
    """Morning volume sits at ~10.5; a huge print at 15:29 (closing-auction window) sits at 30.5."""
    morning = minute_session("2026-09-28", [(10.2, 10.8, 100)] * 3)
    auction_idx = pd.DatetimeIndex([pd.Timestamp("2026-09-28 15:29", tz=IST)])
    auction = pd.DataFrame(
        {"Open": 30.5, "High": 30.8, "Low": 30.2, "Close": 30.5, "Volume": 5000.0},
        index=auction_idx,
    )
    return pd.concat([morning, auction])


class TestContinuousSessionProfile:
    """NSE's closing auction (15:15-15:35 for F&O stocks, live since 2026-08-03) is a separate
    single-price auction. The intraday value area describes continuous trading only."""

    def test_the_profile_covers_continuous_trading_by_default(self):
        out = session_value_areas(session_with_auction_print(), tick=1.0, rows_per_tick=1)
        assert out["poc"].iloc[0] == 10.5

    def test_including_the_auction_is_an_explicit_option(self):
        out = session_value_areas(
            session_with_auction_print(), tick=1.0, rows_per_tick=1, continuous_only=False
        )
        assert out["poc"].iloc[0] == 30.5  # the auction dominates when it is counted

    def test_prev_session_passes_the_option_through(self):
        m1 = pd.concat(
            [session_with_auction_print(), minute_session("2026-09-29", [(20.1, 20.9, 50)])]
        )
        default = prev_session_value_area(m1, m1.index, tick=1.0, rows_per_tick=1)
        with_auction = prev_session_value_area(
            m1, m1.index, tick=1.0, rows_per_tick=1, continuous_only=False
        )
        last = m1.index[-1]
        assert default.loc[last, "ppoc"] == 10.5
        assert with_auction.loc[last, "ppoc"] == 30.5


class TestPrevSessionValueArea:
    def test_first_session_has_no_previous(self):
        m1 = three_sessions()
        out = prev_session_value_area(m1, m1.index, tick=1.0, rows_per_tick=1)
        assert out[day_of(m1.index, "2026-09-28")].isna().all().all()

    def test_each_session_reads_the_prior_session_only(self):
        m1 = three_sessions()
        out = prev_session_value_area(m1, m1.index, tick=1.0, rows_per_tick=1)
        assert (out[day_of(m1.index, "2026-09-29")]["ppoc"] == 11.5).all()
        assert (out[day_of(m1.index, "2026-09-30")]["ppoc"] == 20.5).all()

    def test_output_aligned_to_requested_index(self):
        m1 = three_sessions()
        idx = m1.index[::2]
        out = prev_session_value_area(m1, idx, tick=1.0, rows_per_tick=1)
        assert out.index.equals(idx)
        assert list(out.columns) == ["pvah", "ppoc", "pval"]

    def test_causal_truncation_keeps_history(self):
        m1 = three_sessions()
        full = prev_session_value_area(m1, m1.index, tick=1.0, rows_per_tick=1)
        cut = m1.iloc[:-1]
        part = prev_session_value_area(cut, cut.index, tick=1.0, rows_per_tick=1)
        pd.testing.assert_frame_equal(part, full.loc[part.index])

    def test_empty_session_keeps_older_value_area(self):
        m1 = pd.concat(
            [
                minute_session("2026-09-28", [(10.2, 10.8, 100)]),
                minute_session("2026-09-29", [(20.1, 20.9, 0)]),  # no volume -> no profile
                minute_session("2026-09-30", [(30.1, 30.9, 10)]),
            ]
        )
        out = prev_session_value_area(m1, m1.index, tick=1.0, rows_per_tick=1)
        assert (out[day_of(m1.index, "2026-09-30")]["ppoc"] == 10.5).all()
