"""Shared fixtures for the intake (spec v2) tests."""

from __future__ import annotations

import copy
from typing import Any

GOOD_SPEC: dict[str, Any] = {
    "schema_version": 2,
    "name": "VWAP EMA momentum",
    "source": {"kind": "own_words", "ref": "_inbox/vwap-ema.txt"},
    "profile": "flattrade-nse-cash",
    "style": "intraday",
    "direction": "both",
    "timeframe": "5m",
    "product": "MIS",
    "entry": ["5-min close > VWAP", "EMA9 > EMA20", "Enter at the next candle open"],
    "exit": ["Close below VWAP", "Maximum hold 12 bars"],
    "stop": ["1 ATR(14) below entry"],
    "take_profit": "none",
    "holding_period": "12 bars",
    "indicators": [
        {"name": "VWAP", "anchor": "session"},
        {"name": "EMA", "period": 9, "source": "close"},
        {"name": "EMA", "period": 20},
        {"name": "ATR", "period": 14},
    ],
    "parameters": {"ema_fast": 9, "ema_slow": 20, "atr_len": 14},
    "filters": "none",
    "session_window": {"entry": "09:20-14:30"},
    "re_entry": {"allowed": False},
    "sizing": {"risk_per_trade_pct": 1.0, "capital_per_trade": 100000},
    "portfolio": {"mode": "single_position", "scope": "per_symbol"},
    "disclosure": {"prior_variants": 0, "chart_peeking": False, "destination": "python"},
}

MULTI_POSITION: dict[str, Any] = {
    "mode": "multi_position",
    "max_concurrent": 3,
    "max_trades_per_day": 6,
    "capital_per_position": 100000,
    "max_exposure": 300000,
    "selection": "chronological",
    "conflict": "skip_if_symbol_open",
}


def make_spec(**changes: Any) -> dict[str, Any]:
    """A deep copy of GOOD_SPEC with top-level keys replaced (a value of None deletes the key)."""
    out = copy.deepcopy(GOOD_SPEC)
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = copy.deepcopy(value)
    return out
