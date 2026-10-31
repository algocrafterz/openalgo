"""Previous-session Value Area (POC/VAH/VAL) for backtest strategies.

ONE CALCULATION (BUG-012, 2026-10-31)
    This used the `MarketProfile` library, which bins each 1-minute bar's CLOSE only and sizes
    rows to about 30 per session, so its levels differed from `features/value_area.py` (volume
    spread evenly over each bar's High-Low range, rows of 5 exchange ticks, 70% grown from the
    POC toward the heavier neighbour). A result from one did not transfer to the other. Both now
    use `features/value_area.py`, which is also what the Pine strategy and the stored dataset
    (`features/value_area_store.py`) compute.

    Spreading over the range is the standard approximation when there is no tick data
    (TradingView-style profiles do the same); the close-only bin piles a bar's whole volume on
    one price. Growing one row at a time toward the heavier side is the CQG/NinjaTrader rule;
    the CBOT two-row variant moves VAH/VAL by about a row. Rows are 5 ticks of the NSE tick in
    force on that session's date (`tick_reference`: flat 0.05 until 2024-06-09, tiered after),
    so a 2016 session and a 2026 one are both sized by the exchange's own grid.

WHAT THIS IS NOT
    A TPO (time) profile, or a tick-level volume profile. Only the continuous market is
    profiled (bars before 15:15): since 2026-08-03 NSE's closing auction prints about half the
    day's volume in one bar at the close, which would pin the POC there.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.features.session import continuous_session
from signal_engine.features.value_area import finalize_profile, histogram

#: Rows per exchange tick, as in breakout.pine (klTicksPerRow) and features/value_area.py.
ROWS_PER_TICK = 5

#: Fallback tick when the session date is outside the tick reference.
FALLBACK_TICK = 0.05

#: A session with fewer bars than this (holiday-shortened, bad print) is skipped
#: rather than profiled from noise.
MIN_BARS_PER_SESSION = 5


def session_tick(price: float, day) -> float:
    """The NSE cash tick in force on `day` for `price` (FALLBACK_TICK outside the reference)."""
    from signal_engine.backtest.tick_reference import TickReferenceError, tick_for_price

    try:
        return tick_for_price(float(price), pd.Timestamp(day).date())
    except (TickReferenceError, ValueError):
        return FALLBACK_TICK


def session_value_area(
    df: pd.DataFrame, day: pd.Series, value_area_pct: float = 70.0
) -> pd.DataFrame:
    """One row per session: poc / vah / val, indexed by session date.

    `df` must carry High/Low/Close/Volume and a tz-aware intraday index (i.e. already
    run through `indicators.add_session_columns`). `day` is that function's own `day`
    column - passed separately, as every other indicator here does, rather than
    re-derived.
    """
    df = continuous_session(df)
    day = day.loc[df.index]
    rows: dict = {}
    for d, idx in df.groupby(day).groups.items():
        sub = df.loc[idx]
        if len(sub) < MIN_BARS_PER_SESSION or not (sub["Volume"].sum() > 0):
            continue
        tick = session_tick(sub["Close"].iloc[0], d)
        hist = histogram(
            sub["Low"].to_numpy(),
            sub["High"].to_numpy(),
            sub["Volume"].to_numpy(),
            tick,
            ROWS_PER_TICK,
        )
        vah, poc, val = finalize_profile(hist, value_area_pct / 100.0, tick * ROWS_PER_TICK)
        if not np.isfinite(poc):
            continue
        rows[d] = (poc, val, vah)

    # Force float64 so "no value area" is always a real NaN, never a Python None that
    # survives reindex/shift and fails deep inside a strategy's entry().
    out = pd.DataFrame.from_dict(
        rows, orient="index", columns=["poc", "val", "vah"], dtype="float64"
    )
    out.index.name = "day"
    return out.sort_index()


def value_area_at(
    per_day: pd.DataFrame,
    day: pd.Series,
    lookback: int = 1,
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
    df: pd.DataFrame,
    day: pd.Series,
    value_area_pct: float = 70.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """PPOC / PVAL / PVAH, shifted one session forward and broadcast to every bar
    of the NEXT session - the only form of this a strategy's `prepare()` may read.

    Returns (ppoc, pval, pvah) aligned to df.index. A session with no prior value
    area (first session in the frame, or a skipped short session before it) reads NaN.
    """
    per_day = session_value_area(df, day, value_area_pct)
    return value_area_at(per_day, day, 1)
