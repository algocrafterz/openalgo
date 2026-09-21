"""Indicator and level primitives shared by every strategy adapter.

Each mirrors the PineScript builtin it is named after, including the smoothing
Pine actually uses (Wilder's RMA for atr/rsi/dmi, not a simple mean). Anything that
reads a completed higher-timeframe or previous-day value is shifted so it can never
see the future - the single most common way a backtest invents an edge.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing - what ta.atr, ta.rsi and ta.dmi use internally."""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["Close"].shift(1)
    return pd.concat([df["High"] - df["Low"],
                      (df["High"] - pc).abs(),
                      (df["Low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    down = rma((-d).clip(lower=0), n).replace(0, np.nan)
    return 100 - 100 / (1 + rma(d.clip(lower=0), n) / down)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["High"].diff(), -df["Low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = rma(true_range(df), n).replace(0, np.nan)
    pdi, mdi = 100 * rma(plus, n) / a, 100 * rma(minus, n) / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return rma(dx, n)


def session_vwap(df: pd.DataFrame, day: pd.Series) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    return (tp * df["Volume"]).groupby(day).cumsum() / df["Volume"].groupby(day).cumsum().replace(0, np.nan)


def rvol_time_of_day(df: pd.DataFrame, day: pd.Series, days: int = 14) -> pd.Series:
    """Volume against the SAME session slot on prior days.

    A flat N-bar average is time-blind: at 09:20 its denominator is mostly the previous
    session's dead closing bars while the numerator runs several times normal, so the
    test is near-inert in exactly the window intraday strategies trade most, and
    over-strict around the lunch lull.
    """
    slot = df.groupby(day).cumcount()
    tmp = pd.DataFrame({"d": day.values, "slot": slot.values, "v": df["Volume"].values})
    wide = tmp.pivot_table(index="d", columns="slot", values="v", aggfunc="first")
    base = wide.shift(1).rolling(days, min_periods=3).mean()      # never its own session
    flat = base.stack(future_stack=True).rename("base").reset_index()
    merged = tmp.merge(flat, on=["d", "slot"], how="left")
    return df["Volume"] / pd.Series(merged["base"].values, index=df.index).replace(0, np.nan)


def last_pivots(df: pd.DataFrame, n: int) -> tuple[pd.Series, pd.Series]:
    """Most recent CONFIRMED pivot high / low, carried forward.

    A pivot confirms only n bars after it prints, so the level is shifted by n. That
    lag is inherent to pivots; removing it is lookahead.
    """
    h, low = df["High"], df["Low"]
    ph = h.where(h == h.rolling(2 * n + 1, center=True).max()).shift(n).ffill()
    pl = low.where(low == low.rolling(2 * n + 1, center=True).min()).shift(n).ffill()
    return ph, pl


def prev_day_levels(df: pd.DataFrame, day: pd.Series) -> tuple[pd.Series, pd.Series]:
    d = df.groupby(day).agg(h=("High", "max"), l=("Low", "min"))
    return day.map(d["h"].shift(1)), day.map(d["l"].shift(1))


def initial_balance_ratio(df: pd.DataFrame, day: pd.Series, ib_minutes: int) -> pd.Series:
    """Day range / Initial-Balance range for the PRIOR completed session, shifted one
    session forward and broadcast to every bar of the next session.

    Market Profile's own day-type classification: a Normal/balance day stays close to
    its opening IB range (ratio near 1-2), a Trend day extends well beyond it (ratio
    much larger and typically closes near the extreme). Requires `from_open` from
    `add_session_columns`. NaN for the first session in the frame or a session whose
    IB window (the first `ib_minutes`) has no bars.
    """
    ib_mask = df["from_open"] < ib_minutes
    ib_high = df["High"].where(ib_mask).groupby(day).transform("max")
    ib_low = df["Low"].where(ib_mask).groupby(day).transform("min")
    day_high = df["High"].groupby(day).transform("max")
    day_low = df["Low"].groupby(day).transform("min")
    ib_range = (ib_high - ib_low).replace(0, np.nan)
    ratio = (day_high - day_low) / ib_range
    per_day = ratio.groupby(day).first()
    return day.map(per_day.shift(1))


def prev_daily_trend_z(df: pd.DataFrame, day: pd.Series, ema_len: int,
                        range_len: int = 14) -> pd.Series:
    """How far the PRIOR session's close sits from a daily EMA of closes, in units of
    the recent average daily range - shifted one session forward and broadcast to
    every bar of the next session.

    Positive = prior close above its daily EMA (uptrend); negative = below
    (downtrend); magnitude is in "typical day ranges", so it is comparable across
    symbols at very different price levels rather than being a raw price distance.
    NaN until `max(ema_len, range_len)` sessions of history exist.
    """
    daily_close = df["Close"].groupby(day).last()
    daily_ema = daily_close.ewm(span=ema_len, adjust=False).mean()
    daily_range = df["High"].groupby(day).max() - df["Low"].groupby(day).min()
    daily_atr = daily_range.rolling(range_len, min_periods=5).mean().replace(0, np.nan)
    z = (daily_close - daily_ema) / daily_atr
    return day.map(z.shift(1))


def opening_range(df: pd.DataFrame, day: pd.Series, minutes: int) -> tuple[pd.Series, pd.Series]:
    """Rolling OR high/low. Requires the `from_open` column from add_session_columns."""
    inside = df["from_open"] < minutes
    return (df["High"].where(inside).groupby(day).cummax().ffill(),
            df["Low"].where(inside).groupby(day).cummin().ffill())


def htf_bias(df: pd.DataFrame, mult: int, length: int) -> tuple[pd.Series, pd.Series]:
    """Higher-timeframe direction, shifted to the PREVIOUS completed HTF bar."""
    grp = np.arange(len(df) - 1, -1, -1) // mult
    htf_close = df["Close"].iloc[::-1].groupby(grp).transform("last").iloc[::-1]
    htf_ema = ema(df["Close"], length * mult)
    return htf_close.shift(mult) > htf_ema.shift(mult), htf_close.shift(mult) < htf_ema.shift(mult)


def bars_since(flag: pd.Series) -> pd.Series:
    idx = np.arange(len(flag))
    last = pd.Series(np.where(flag.to_numpy(), idx, np.nan)).ffill().to_numpy()
    return pd.Series(np.where(np.isnan(last), 9999, idx - last), index=flag.index)


def add_session_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise the index to IST and attach the session bookkeeping the engine needs."""
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(
        subset=["Open", "High", "Low", "Close"]).copy()
    day = pd.Series(df.index.date, index=df.index)
    df["day"] = day
    df["mins"] = df.index.hour * 60 + df.index.minute
    df["new_session"] = (day != day.shift(1)).to_numpy()
    # Minutes since this session's first bar. The engine gates the opening skip on it,
    # and it is derived from the data rather than hardcoded to 09:15 so the same code
    # works on a half-day or a symbol whose first print is late.
    df["from_open"] = df["mins"] - day.map(df.groupby(day)["mins"].min())
    return df
