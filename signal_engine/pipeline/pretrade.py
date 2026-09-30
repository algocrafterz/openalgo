"""Shared pre-trade gates: the validator's stateless checks plus window, staleness, price band.

The validator's stateful duplicate check is deliberately excluded; the pipeline's own
idempotency store (state.py) owns "have we seen this bar" for runner-sourced signals.
"""

from __future__ import annotations

import pandas as pd

from signal_engine import validator
from signal_engine.config import settings
from signal_engine.models import Signal, ValidationResult

_CHECKS = (
    validator._check_blacklist,
    validator._check_prices_positive,
    validator._check_price_ordering,
    validator._check_reward_risk,
    validator._check_sl_distance,
)


def check(signal: Signal) -> ValidationResult | None:
    """First failing stateless check, or None when the signal may proceed."""
    for fn in _CHECKS:
        result = fn(signal)
        if result is not None:
            return result
    return None


def in_entry_window(bar_time: pd.Timestamp, window_min: tuple[int, int]) -> bool:
    """True when the bar's open time falls in [start, cutoff), minutes after midnight IST."""
    minutes = bar_time.hour * 60 + bar_time.minute
    return window_min[0] <= minutes < window_min[1]


def is_stale(bar_close: pd.Timestamp, now: pd.Timestamp, limit_seconds: float) -> bool:
    return (now - bar_close).total_seconds() > limit_seconds


def price_band(strategy: str) -> tuple[float, float]:
    """(min, max) entry price: the strategy profile override, else the global sizing band."""
    profile = settings.strategy_profiles.get(strategy.upper(), {})
    lo = profile.get("min_entry_price", settings.min_entry_price)
    hi = profile.get("max_entry_price", settings.max_entry_price)
    return float(lo), float(hi)


def ineligible_reason(price: float, min_price: float, max_price: float) -> str | None:
    """Why a symbol at `price` cannot be traded, or None. A zero bound means no limit."""
    if not price > 0:
        return f"non-positive price {price}"
    if min_price > 0 and price < min_price:
        return f"price {price} below minimum {min_price}"
    if max_price > 0 and price > max_price:
        return f"price {price} above maximum {max_price}"
    return None
