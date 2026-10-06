"""Price indicators every intraday strategy reuses: ATR, EMA, session VWAP, CLV.

Semantics follow the Pine builtins:
  * `rma` is Wilder's smoothing (ta.rma), what ta.atr uses internally.
  * `ema` is seeded on the first value and runs continuously across sessions.
  * `session_vwap` is anchored to each session and volume-weights the chosen source
    (`ta.vwap(close)` in breakout.pine weights the CLOSE, hence the default).
  * `atr` runs continuously across sessions, so the overnight gap enters the true range.

All functions are causal: the value at bar k depends only on bars <= k.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (ta.rma)."""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average seeded on the first value (ta.ema)."""
    return s.ewm(span=n, adjust=False).mean()


def ema_slope(s: pd.Series, n: int) -> pd.Series:
    """ema - ema[1]; 0 on the first bar, as Pine's nz(ema[1], ema) gives."""
    e = ema(s, n)
    return (e - e.shift(1)).fillna(0.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average true range (ta.atr): Wilder smoothing of the true range."""
    return rma(true_range(df), n)


def session_vwap(df: pd.DataFrame, source: str = "Close") -> pd.Series:
    """Session-anchored VWAP of `source` ("Close" or "hlc3"); NaN while cumulative volume is 0."""
    if source == "hlc3":
        price = (df["High"] + df["Low"] + df["Close"]) / 3
    else:
        price = df[source]
    day = pd.Series(df.index.date, index=df.index)
    weighted = (price * df["Volume"]).groupby(day).cumsum()
    volume = df["Volume"].groupby(day).cumsum().replace(0, np.nan)
    return weighted / volume


def clv(df: pd.DataFrame) -> pd.Series:
    """Close location value in [0, 1]: where the close sits inside the bar; 0.5 for a flat bar."""
    rng = df["High"] - df["Low"]
    return ((df["Close"] - df["Low"]) / rng.where(rng > 0)).fillna(0.5)


def session_vwap_sd(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Session VWAP of hlc3 and its volume-weighted standard deviation.

    tp = (High + Low + Close) / 3; VWAP = sum(v * tp) / sum(v);
    SD = sqrt(max(sum(v * tp^2) / sum(v) - VWAP^2, 0)), both accumulated from the session's
    first bar and including the current one. NaN while cumulative volume is 0.
    """
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    day = pd.Series(df.index.date, index=df.index)
    volume = df["Volume"].groupby(day).cumsum().replace(0, np.nan)
    vwap = (tp * df["Volume"]).groupby(day).cumsum() / volume
    second = (tp * tp * df["Volume"]).groupby(day).cumsum() / volume
    sd = np.sqrt((second - vwap * vwap).clip(lower=0.0))
    return vwap, sd


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """ADX (ta.dmi's adx): Wilder-smoothed DX, running continuously across sessions."""
    up, dn = df["High"].diff(), -df["Low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = rma(true_range(df), n).replace(0, np.nan)
    pdi, mdi = 100 * rma(plus, n) / a, 100 * rma(minus, n) / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return rma(dx, n)
