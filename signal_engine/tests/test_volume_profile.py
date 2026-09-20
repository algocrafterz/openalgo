"""Verifies the `MarketProfile`-library wiring, not the value-area algorithm itself
(the library owns that). What can still be wrong here: bucket sizing, session
grouping, and the one-session shift - the same class of bug
`test_backtest_integrity.py` exists to pin for the engine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest import volume_profile as vp

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
    # Hand-traced: POC=101.0 (vol 1500). Step 1: high-side neighbour 101.5 (800) >
    # low-side 100.5 (450) -> take high, running 2300. Step 2: next high 102.0 (600)
    # > next low 100.5 (450) -> take high, running 2900 >= 70% of 3550 (2485) -> stop.
    # The low side is never touched, so VAL stays pinned at the POC.
    assert row["poc"] == 101.0
    assert row["val"] == 101.0
    assert row["vah"] == 102.0


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
    assert (second_day == 101.0).all()
    pv = pval.loc[day == datetime.date(2026, 6, 2)]
    vh = pvah.loc[day == datetime.date(2026, 6, 2)]
    assert (pv == 101.0).all() and (vh == 101.0).all()   # heavy_day's POC==VAL==VAH


def test_zero_volume_bucket_between_poc_and_edge_is_skipped_not_crashed():
    # Reproduces a real MarketProfile library bug (see volume_profile.py's comment):
    # a LEGITIMATE zero-volume bucket sitting between the POC and one edge is
    # misread as "no buckets left on this side" (bare truthiness, 0 vs None), which
    # stalls that side until the other one exhausts for real and crashes on
    # `int + None`. Confirmed against real data: UNIONBANK 2026-09-08 had a
    # zero-volume bucket at Rs 183.30 between its POC (182.0) and session high.
    # Minimal repro: POC in the middle, a zero bucket just above it, and too little
    # volume below the POC to reach the target alone - so the low side exhausts for
    # real while the high side is still stalled on the zero bucket, and the library
    # tries to add the resulting `None` to the running total.
    prices = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5]
    volumes = [5, 5, 100, 0, 50, 50]   # POC=100.2 (idx2); 100.3 is the zero bucket
    df = _session("2026-06-01", prices, volumes)
    day = pd.Series(df.index.date, index=df.index)

    out = vp.session_value_area(df, day, value_area_pct=70.0)

    assert len(out) == 0   # skipped, not crashed and not silently wrong


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
