"""IB width relative to the stock's recent daily range: the BreakingTrade website's IB% column.

ib_pct = 100 x (IB high - IB low) / mean(high - low) of the previous six completed sessions,
with the IB the first 60 minutes from 09:15 IST. The site does not publish its formula; this one
was fitted against 6,503 of its snapshots (median absolute error 3.7 points, rank correlation
0.94, the same narrow/mid/wide third in 85 percent of cases). See
strategy_library/intraday/breaking-trade/experiments/2026-10-ib-pct-library/README.md.

Built from levels.initial_balance and levels.adr_prev, so it is causal in the same way: the
average reads completed sessions only, and the IB is a running value that freezes at 10:15.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.features.levels import _day, adr_prev, initial_balance

IB_PCT_SESSIONS = 6
IB_MINUTES = 60


def ib_width(
    df: pd.DataFrame,
    n: int = IB_PCT_SESSIONS,
    minutes: int = IB_MINUTES,
    min_ib_bars: int = 1,
) -> pd.DataFrame:
    """Per bar: ib_high, ib_low, ib_range, ib_bars, ib_done, adr and ib_pct.

    Bars are stamped by their open time on an IST index and may be any timeframe. A bar's
    value includes that bar, so it is known once the bar closes. ib_pct is NaN until n prior
    sessions exist, and while fewer than min_ib_bars IB bars have printed.
    """
    ib = initial_balance(df, minutes=minutes)
    inside = ~ib["ib_done"]
    day = _day(df)
    ib_bars = inside.astype(int).groupby(day).cumsum()
    ib_range = ib["ib_high"] - ib["ib_low"]
    adr = adr_prev(df, n=n)
    pct = (100.0 * ib_range / adr).where(ib_bars >= min_ib_bars)
    return pd.DataFrame(
        {
            "ib_high": ib["ib_high"],
            "ib_low": ib["ib_low"],
            "ib_range": ib_range,
            "ib_bars": ib_bars,
            "ib_done": ib["ib_done"],
            "adr": adr,
            "ib_pct": pct,
        },
        index=df.index,
    )


def ib_pct_at(
    df: pd.DataFrame,
    at: pd.Timestamp,
    bar_minutes: int,
    n: int = IB_PCT_SESSIONS,
    minutes: int = IB_MINUTES,
) -> float | None:
    """IB% as it stood at `at`, from bars that had closed by then; None if not yet known.

    Only bars of `at`'s own session that closed at or before `at` count, so the answer for a
    signal time never uses a later bar.
    """
    closed = df[df.index + pd.Timedelta(minutes=bar_minutes) <= at]
    if closed.empty or closed.index[-1].date() != at.date():
        return None
    value = ib_width(closed, n=n, minutes=minutes)["ib_pct"].iloc[-1]
    return None if pd.isna(value) else float(value)
