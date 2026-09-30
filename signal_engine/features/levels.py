"""Session levels every intraday strategy reuses: PDH/PDL, ADR, day extremes, IB, opening range.

Session boundaries come from the IST index date. Everything is causal: prior-session values
(PDH/PDL, ADR) read completed sessions only, and IB / OR are running values that freeze when
their window closes, so no bar sees data from its own future.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SESSION_OPEN_MINUTE = 9 * 60 + 15


def _day(df: pd.DataFrame) -> pd.Series:
    return pd.Series(df.index.date, index=df.index)


def _minutes_from_open(df: pd.DataFrame) -> np.ndarray:
    return (df.index.hour * 60 + df.index.minute).to_numpy() - SESSION_OPEN_MINUTE


def prev_day_hl(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Previous completed session's (high, low) on every bar; NaN on the first session."""
    day = _day(df)
    daily = df.groupby(day).agg(h=("High", "max"), l=("Low", "min"))
    return day.map(daily["h"].shift(1)).rename("pdh"), day.map(daily["l"].shift(1)).rename("pdl")


def adr_prev(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Mean daily range (high - low) of the n sessions BEFORE today; NaN until n exist.

    Matches `request.security("D", ta.sma(high - low, n)[1], lookahead_on)`.
    """
    day = _day(df)
    daily = df.groupby(day).agg(h=("High", "max"), l=("Low", "min"))
    ranges = (daily["h"] - daily["l"]).rolling(n, min_periods=n).mean().shift(1)
    return day.map(ranges).rename("adr")


def day_extremes(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Running session (high, low) including the current bar."""
    day = _day(df)
    return (
        df["High"].groupby(day).cummax().rename("day_high"),
        df["Low"].groupby(day).cummin().rename("day_low"),
    )


def _window_extremes(df: pd.DataFrame, inside: np.ndarray) -> tuple[pd.Series, pd.Series]:
    """Running high/low of the bars inside a window, carried forward after it closes."""
    day = _day(df)
    high = df["High"].where(inside).groupby(day).cummax().groupby(day).ffill()
    low = df["Low"].where(inside).groupby(day).cummin().groupby(day).ffill()
    return high, low


def initial_balance(df: pd.DataFrame, minutes: int = 60) -> pd.DataFrame:
    """Initial balance: running high/low of the first `minutes` of the session.

    Returns ib_high, ib_low (running while the window is open, frozen after it) and ib_done,
    True from the first bar AFTER the window. Bars are stamped by their open time, so a
    60-minute IB covers the bars opening 09:15 .. 10:10.
    """
    inside = _minutes_from_open(df) < minutes
    high, low = _window_extremes(df, inside)
    return pd.DataFrame({"ib_high": high, "ib_low": low, "ib_done": ~inside}, index=df.index)


def opening_range(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """Opening range high/low, NaN while it is still forming, frozen once it has formed."""
    inside = _minutes_from_open(df) < minutes
    high, low = _window_extremes(df, inside)
    formed = ~inside
    return pd.DataFrame(
        {"orb_high": high.where(formed), "orb_low": low.where(formed)}, index=df.index
    )
