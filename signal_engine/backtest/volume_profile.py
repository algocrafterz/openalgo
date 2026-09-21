"""Previous-session Value Area (POC/VAH/VAL), via the `MarketProfile` library.

WHY A LIBRARY INSTEAD OF HAND-ROLLED BINNING
    `key_level.py`'s docstring flags PVAH/PPOC/PVAL as "left untested rather than
    approximated" because yfinance only serves 7 days of 1-minute history. That
    blocker does not apply here: `data.from_historify(interval="1m")` reads years of
    1-minute bars out of `historify.duckdb`. `pandas-ta`'s `vp()` bins volume by price
    but does not compute a value area; `MarketProfile` (PyPI, bfolkens/py-market-
    profile) does the actual 70%-of-volume-from-POC-outward calculation, so this
    module is a thin wrapper rather than a second implementation of that algorithm.

WHAT THIS IS NOT
    `MarketProfile`'s "vol" mode buckets each bar's CLOSE price and sums its Volume
    into that one bucket - it does not distribute a bar's volume across its High-Low
    range, and it is not a tick-level TPO profile. On 1-minute bars the close-only
    approximation is reasonable (a bar's range is narrow); it would be a much rougher
    approximation on 5-minute bars. Treat this as a volume-profile approximation, not
    a terminal-grade Market Profile.

BUCKET SIZING
    NSE's exchange tick is a flat Rs 0.05 regardless of price. Slicing a session at
    that raw tick on a Rs 3,850 stock (TCS) with a Rs 50-80 daily range produces
    ~1,000 nearly-empty buckets - too fine to summarize a distribution. `_prices_per_row`
    instead sizes the bucket so each session gets roughly `TARGET_ROWS` buckets,
    regardless of the stock's absolute price - the standard practice for volume
    profile, not a workaround.

SHIFT DISCIPLINE
    `prev_session_value_area()` returns each day's value area attached to the NEXT
    session's bars only, mirroring `indicators.prev_day_levels`. A strategy's
    `prepare()` must use the shifted output; using `session_value_area()` directly
    inside entry() would be lookahead - today's own value area is not known until the
    session closes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from market_profile import MarketProfile

#: NSE equity tick size - uniform regardless of price.
TICK_SIZE = 0.05

#: Buckets to aim for per session. 25-40 is the usual range quoted for volume
#: profile; 30 sits in the middle regardless of the stock's price level.
TARGET_ROWS = 30

#: A session with fewer bars than this (holiday-shortened, bad print) is skipped
#: rather than fed to MarketProfile, which would build a profile from noise.
MIN_BARS_PER_SESSION = 5


def _prices_per_row(day_high: float, day_low: float) -> int:
    """MarketProfile's `row_size = tick_size * prices_per_row` - solved for the
    integer `prices_per_row` that gives roughly TARGET_ROWS buckets across the
    session's own High-Low range."""
    rng = day_high - day_low
    if not np.isfinite(rng) or rng <= 0:
        return 1
    return max(1, round(rng / TICK_SIZE / TARGET_ROWS))


def session_value_area(df: pd.DataFrame, day: pd.Series,
                        value_area_pct: float = 70.0) -> pd.DataFrame:
    """One row per session: poc / vah / val, indexed by session date.

    `df` must carry High/Low/Close/Volume and a tz-aware intraday index (i.e. already
    run through `indicators.add_session_columns`). `day` is that function's own `day`
    column - passed separately, as every other indicator here does, rather than
    re-derived.
    """
    rows: dict = {}
    for d, idx in df.groupby(day).groups.items():
        sub = df.loc[idx]
        if len(sub) < MIN_BARS_PER_SESSION:
            continue
        ppr = _prices_per_row(sub["High"].max(), sub["Low"].min())
        mp = MarketProfile(df, tick_size=TICK_SIZE, prices_per_row=ppr,
                            value_area_pct=value_area_pct / 100.0, mode="vol")
        try:
            s = mp[idx[0]:idx[-1]]
        except TypeError:
            # A real bug in `MarketProfile.calculate_value_area()`: it tests "no
            # buckets left on this side" with bare Python truthiness (`not x`), which
            # cannot tell a genuinely EMPTY side (x is None) from a bucket that has
            # legitimate zero volume (x == 0, e.g. an illiquid minute) - both read as
            # falsy. A zero-volume bucket between the POC and one edge permanently
            # stalls that side's expansion; the OTHER side then exhausts for real,
            # hands back a genuine None, and the library unconditionally adds it to
            # the running total. Confirmed against UNIONBANK 2026-09-08 (a 0-volume
            # bucket at Rs 183.30, between the POC and the session high). Not rare on
            # real NSE 1-minute data. Skip the session rather than patch third-party
            # internals - same treatment as a too-short session below.
            continue
        if s.poc_price is None:
            continue
        val, vah = s.value_area
        rows[d] = (s.poc_price, val, vah)

    # `from_dict` on an EMPTY dict (every session skipped - too short, or the library
    # bug above) infers object dtype filled with Python `None`, not float NaN. That
    # silently survives `reindex`/`shift` in `prev_session_value_area` and then blows
    # up `np.isfinite(None)` deep inside a strategy's entry() - far from this file.
    # Force float64 unconditionally so "no value area" is always a real NaN.
    out = pd.DataFrame.from_dict(rows, orient="index", columns=["poc", "val", "vah"],
                                 dtype="float64")
    out.index.name = "day"
    return out.sort_index()


def value_area_at(
    per_day: pd.DataFrame, day: pd.Series, lookback: int = 1,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """POC/VAL/VAH from `lookback` sessions back, broadcast to every bar of the
    session `lookback` ahead - the only form of a value area a strategy's `prepare()`
    may read (reading the CURRENT session's own value area inside entry() would be
    lookahead, since it is not final until the session closes).

    `per_day` is `session_value_area()`'s own output - passed in rather than
    recomputed here so a caller needing more than one lookback (e.g. multi-day
    confluence: this session's value area AND the one before it) pays for the
    expensive MarketProfile reconstruction once, not once per lookback.

    Returns (poc, val, vah) aligned to df.index. A session with no value area that far
    back (start of the frame, or a skipped short session in the gap) reads NaN.
    """
    # Reindex onto EVERY day present in the frame, not just the ones that produced a
    # profile - otherwise a day with too few bars to build its own value area (a
    # holiday-shortened session) is simply absent as a row, `shift(n)` walks that gap
    # by ROW POSITION rather than by calendar day, and the day AFTER it silently reads
    # the wrong value area (or, if it is the last day in the frame, a KeyError from
    # `day.map` finding no row for its own date at all).
    all_days = pd.Index(sorted(pd.unique(day)), name="day")
    shifted = per_day.reindex(all_days).shift(lookback)
    poc = day.map(shifted["poc"])
    val = day.map(shifted["val"])
    vah = day.map(shifted["vah"])
    return poc, val, vah


def prev_session_value_area(
    df: pd.DataFrame, day: pd.Series, value_area_pct: float = 70.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """PPOC / PVAL / PVAH, shifted one session forward and broadcast to every bar
    of the NEXT session - the only form of this a strategy's `prepare()` may read.

    Returns (ppoc, pval, pvah) aligned to df.index. A session with no prior value
    area (first session in the frame, or a skipped short session before it) reads NaN.
    """
    per_day = session_value_area(df, day, value_area_pct)
    return value_area_at(per_day, day, 1)
