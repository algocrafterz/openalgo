"""Assemble the BREAKOUT core's input frame from broker bars, using the shared features.

This module only COMPOSES `signal_engine.features`; it computes nothing itself, so every
input is the same reusable, tested function any other strategy would call. Feed it broker
data (Historify 5-minute bars, plus the 1-minute bars the value area needs) exactly as it is.

Every column is causal, so the same builder serves a full-history backtest and a rolling
live window.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.features.htf import htf_prev_close
from signal_engine.features.levels import (
    adr_prev,
    day_extremes,
    initial_balance,
    opening_range,
    prev_day_hl,
)
from signal_engine.features.price import atr, ema, ema_slope, session_vwap
from signal_engine.features.value_area import prev_session_value_area
from signal_engine.features.volume import session_volume_factor

INPUT_COLUMNS = (
    "Close",
    "High",
    "Low",
    "atr",
    "vwap",
    "ema9",
    "ema_slope",
    "rvol",
    "sess_vf",
    "htf_close",
    "pdh",
    "pdl",
    "ib_high",
    "ib_low",
    "ib_done",
    "orb_high",
    "orb_low",
    "pvah",
    "ppoc",
    "pval",
    "adr",
    "day_high",
    "day_low",
)


def build_breakout_inputs(
    df5m: pd.DataFrame,
    df1m: pd.DataFrame,
    tick: float,
    *,
    atr_len: int = 14,
    ema_len: int = 9,
    ib_minutes: int = 60,
    orb_minutes: int = 15,
    adr_days: int = 14,
    volume_days: int = 14,
    htf_minutes: int = 15,
    rows_per_tick: int = 5,
    va_pct: float = 70.0,
    continuous_only: bool = True,
) -> pd.DataFrame:
    """Return the per-bar input frame for strategy_core.breakout.compute_signals.

    df5m   in-session 5-minute broker bars (Open/High/Low/Close/Volume, IST index)
    df1m   in-session 1-minute broker bars for the same symbol (value-area profile)
    tick   broker tick size (SymToken tick_size); sets the value-area row step
    """
    pdh, pdl = prev_day_hl(df5m)
    day_high, day_low = day_extremes(df5m)
    ib = initial_balance(df5m, ib_minutes)
    orb = opening_range(df5m, orb_minutes)
    vol = session_volume_factor(df5m, days=volume_days)
    va = prev_session_value_area(df1m, df5m.index, tick, rows_per_tick, va_pct, continuous_only)

    out = pd.DataFrame(index=df5m.index)
    out["Close"], out["High"], out["Low"] = df5m["Close"], df5m["High"], df5m["Low"]
    out["atr"] = atr(df5m, atr_len)
    out["vwap"] = session_vwap(df5m)
    out["ema9"] = ema(df5m["Close"], ema_len)
    out["ema_slope"] = ema_slope(df5m["Close"], ema_len)
    out["rvol"] = (df5m["Volume"] / vol["bar_baseline"]).fillna(0.0)
    out["sess_vf"] = vol["vf"]
    out["htf_close"] = htf_prev_close(df5m, htf_minutes)
    out["pdh"], out["pdl"] = pdh, pdl
    out["ib_high"], out["ib_low"], out["ib_done"] = ib["ib_high"], ib["ib_low"], ib["ib_done"]
    out["orb_high"], out["orb_low"] = orb["orb_high"], orb["orb_low"]
    out["pvah"], out["ppoc"], out["pval"] = va["pvah"], va["ppoc"], va["pval"]
    out["adr"] = adr_prev(df5m, adr_days)
    out["day_high"], out["day_low"] = day_high, day_low
    return out[list(INPUT_COLUMNS)]
