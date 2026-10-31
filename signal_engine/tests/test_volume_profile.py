"""Verifies the backtest wiring of the one value-area calculation (features/value_area.py):
session grouping, the date's tick, and the one-session shift - the same class of bug
`test_backtest_integrity.py` exists to pin for the engine. Prices are row centres, so a level
is checked to within one row (ROW: 5 ticks of 0.01 at these prices, NSE tiers since 2024-06-10).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import volume_profile as vp

ROW = 0.05

IST = "Asia/Kolkata"


def _session(day: str, prices: list[float], volumes: list[float]) -> pd.DataFrame:
    """One bar per (price, volume) pair, 1-minute apart from 09:15. High/Low sit a
    fixed 0.05 (one tick) either side of Close so the day's own range - used only to
    size the bucket, not to place bars in it - stays proportionate."""
    idx = pd.date_range(f"{day} 09:15", periods=len(prices), freq="1min", tz=IST)
    return pd.DataFrame({
        "Open": prices, "High": [p + 0.05 for p in prices],
        "Low": [p - 0.05 for p in prices], "Close": prices, "Volume": volumes,
    }, index=idx)


def test_value_area_expands_toward_the_heavier_side():
    # Five price levels, deliberately asymmetric volume so the value area must
    # expand ONLY upward from the POC - a directional check, not a symmetric one.
    #   100.0: 200   100.5: 450   101.0: 1500 (POC)   101.5: 800   102.0: 600
    prices = [100.0] * 2 + [100.5] * 3 + [101.0] * 5 + [101.5] * 4 + [102.0] * 6
    volumes = [100] * 2 + [150] * 3 + [300] * 5 + [200] * 4 + [100] * 6
    df = _session("2026-06-01", prices, volumes)
    day = pd.Series(df.index.date, index=df.index)

    out = vp.session_value_area(df, day, value_area_pct=70.0)

    assert len(out) == 1
    row = out.iloc[0]
    # POC is the 101.0 row (vol 1500). The heavier neighbours are always above it (101.5:
    # 800, 102.0: 600 against 100.5: 450), so the area grows upward only until it holds 70%
    # and VAL stays pinned at the POC row.
    assert row["poc"] == pytest.approx(101.0, abs=ROW)
    assert row["val"] == row["poc"]
    assert row["vah"] == pytest.approx(102.0, abs=ROW)


def test_value_area_shifts_one_session_forward():
    heavy_day = _session("2026-06-01", [100.0] * 2 + [101.0] * 10 + [102.0] * 2,
                         [100] * 2 + [1000] * 10 + [100] * 2)
    next_day = _session("2026-06-02", [105.0] * 6, [200] * 6)
    df = pd.concat([heavy_day, next_day]).sort_index()
    day = pd.Series(df.index.date, index=df.index)

    ppoc, pval, pvah = vp.prev_session_value_area(df, day, value_area_pct=70.0)

    import datetime
    first_day = ppoc.loc[day == datetime.date(2026, 6, 1)]
    second_day = ppoc.loc[day == datetime.date(2026, 6, 2)]
    # The first session in the frame has no prior day - must read NaN, not 0 or a
    # silently-reused value (that would be lookahead into the session's own profile).
    assert first_day.isna().all()
    # The second session reads the FIRST session's value area, broadcast across
    # every bar of the second session (not just its first bar).
    assert second_day.nunique() == 1 and second_day.iloc[0] == pytest.approx(101.0, abs=ROW)
    pv = pval.loc[day == datetime.date(2026, 6, 2)]
    vh = pvah.loc[day == datetime.date(2026, 6, 2)]
    # heavy_day's area is the 101.0 bars alone (their High-Low spans two rows)
    assert (pv == second_day).all() and (vh - pv <= 2 * ROW).all()


def test_value_area_at_two_sessions_back_for_multi_day_confluence():
    day1 = _session("2026-06-01", [100.0] * 2 + [101.0] * 10 + [102.0] * 2,
                    [100] * 2 + [1000] * 10 + [100] * 2)
    day2 = _session("2026-06-02", [200.0] * 6, [200] * 6)
    day3 = _session("2026-06-03", [300.0] * 6, [200] * 6)
    df = pd.concat([day1, day2, day3]).sort_index()
    day = pd.Series(df.index.date, index=df.index)

    per_day = vp.session_value_area(df, day, value_area_pct=70.0)
    poc1, val1, vah1 = vp.value_area_at(per_day, day, 1)
    poc2, val2, vah2 = vp.value_area_at(per_day, day, 2)

    import datetime
    third_day = datetime.date(2026, 6, 3)
    # N-1 (day3 reads day2's flat 200.0 profile) vs N-2 (day3 reads day1's 101.0
    # profile) must disagree - proof the two lookbacks aren't accidentally aliased
    # to the same shift.
    assert poc1.loc[day == third_day].iloc[0] == pytest.approx(200.0, abs=ROW)
    assert poc2.loc[day == third_day].iloc[0] == pytest.approx(101.0, abs=ROW)
    assert val1.loc[day == third_day].iloc[0] == pytest.approx(200.0, abs=ROW)
    # day1's heavy middle bars (10000 vol) alone clear 70% of its 10400 total.
    assert vah2.loc[day == third_day].iloc[0] == pytest.approx(101.0, abs=2 * ROW)
    # Not enough history two sessions back for day2 - must read NaN, not day1 again.
    second_day = datetime.date(2026, 6, 2)
    assert poc2.loc[day == second_day].isna().all()


def test_a_zero_volume_row_between_poc_and_edge_is_profiled_not_skipped():
    # The old MarketProfile library stalled on a legitimate zero-volume bucket (UNIONBANK
    # 2026-09-08, Rs 183.30) and the session had to be skipped. The standard calculation
    # grows past an empty row like any other, so the session now has a value area.
    prices = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5]
    volumes = [5, 5, 100, 0, 50, 50]   # POC=100.2 (idx2); 100.3 is the zero bucket
    df = _session("2026-06-01", prices, volumes)
    day = pd.Series(df.index.date, index=df.index)

    out = vp.session_value_area(df, day, value_area_pct=70.0)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["val"] <= row["poc"] <= row["vah"]
    assert row["poc"] == pytest.approx(100.2, abs=ROW)


def test_short_session_is_skipped_not_approximated():
    short_day = _session("2026-06-01", [100.0, 100.5, 101.0], [10, 10, 10])  # < MIN_BARS
    normal_day = _session("2026-06-02", [100.0] * 10, [10] * 10)
    df = pd.concat([short_day, normal_day]).sort_index()
    day = pd.Series(df.index.date, index=df.index)

    out = vp.session_value_area(df, day)

    assert len(out) == 1   # only the normal session produced a profile


def test_all_sessions_skipped_yields_real_nan_not_none():
    # A symbol where EVERY session is too short to build a profile - the empty-dict
    # path. `pd.DataFrame.from_dict({}, ...)` infers object dtype filled with Python
    # `None` unless forced to float64, and `None` crashes `np.isfinite()` far away in
    # a strategy's entry(), not here - hence asserting the actual numpy dtype and
    # that `isfinite` behaves, not just that the value "looks like" NaN.
    only_short_day = _session("2026-06-01", [100.0, 100.5], [10, 10])   # < MIN_BARS
    df = pd.concat([only_short_day,
                    _session("2026-06-02", [101.0, 101.5], [10, 10])]).sort_index()
    day = pd.Series(df.index.date, index=df.index)

    out = vp.session_value_area(df, day)
    assert len(out) == 0
    assert out["poc"].dtype == np.float64

    ppoc, pval, pvah = vp.prev_session_value_area(df, day)
    assert ppoc.dtype == np.float64
    assert not np.isfinite(ppoc.iloc[0])


def test_the_closing_auction_print_does_not_set_the_poc():
    """Since 2026-08-03 NSE's closing auction shows as one bar stamped about 15:28 holding about
    half the day's volume at the close. Profiles use the continuous market only (bars before
    15:15, features/session.py), so that print must not become the POC."""
    continuous = _session("2026-10-21", [100.0] * 2 + [101.0] * 10 + [102.0] * 2,
                          [100] * 2 + [1000] * 10 + [100] * 2)
    auction_at = pd.Timestamp("2026-10-21 15:28", tz=IST)
    auction = pd.DataFrame({"Open": [104.0], "High": [104.05], "Low": [103.95],
                            "Close": [104.0], "Volume": [50_000]}, index=[auction_at])
    df = pd.concat([continuous, auction]).sort_index()
    day = pd.Series(df.index.date, index=df.index)

    out = vp.session_value_area(df, day, value_area_pct=70.0)

    assert out.iloc[0]["poc"] == pytest.approx(101.0, abs=ROW)
    assert out.iloc[0]["vah"] < 104.0
