"""Signal validation against trading rules and risk constraints."""

import time
from typing import Dict, Tuple

from loguru import logger

from signal_engine.config import settings
from signal_engine.models import Direction, Signal, ValidationResult, ValidationStatus

# In-memory duplicate tracker: (symbol, direction, entry) -> timestamp
_recent_signals: Dict[Tuple[str, str, float], float] = {}


def _cleanup_stale_entries() -> None:
    now = time.time()
    stale_keys = [
        k for k, ts in _recent_signals.items()
        if now - ts > settings.duplicate_window_seconds
    ]
    for k in stale_keys:
        del _recent_signals[k]


def validate(signal: Signal) -> ValidationResult:
    """Validate a signal against trading rules.

    Checks (in order):
    1. Entry > 0
    2. SL > 0
    3. Target > 0
    4. SL direction consistency
    5. Target direction consistency
    6. Minimum R:R ratio
    7. Duplicate detection
    """
    # Entry validity
    if signal.entry <= 0:
        return ValidationResult(status=ValidationStatus.INVALID, reason="Entry must be positive")

    # EXIT signals: minimal validation (closing, not opening)
    # Skip SL/TP/R:R/duplicate checks — EXIT carries original entry data for audit only
    if signal.direction == Direction.EXIT:
        if not signal.symbol or signal.symbol.strip() == "":
            return ValidationResult(status=ValidationStatus.INVALID, reason="EXIT: symbol required")
        return ValidationResult(status=ValidationStatus.VALID)

    # SL validity
    if signal.sl <= 0:
        return ValidationResult(status=ValidationStatus.INVALID, reason="SL must be positive")

    # TP validity
    if signal.tp <= 0:
        return ValidationResult(status=ValidationStatus.INVALID, reason="TP must be positive")

    # SL direction check
    if signal.direction == Direction.LONG and signal.sl >= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="LONG: SL must be below entry"
        )
    if signal.direction == Direction.SHORT and signal.sl <= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="SHORT: SL must be above entry"
        )

    # Target direction check
    if signal.direction == Direction.LONG and signal.tp <= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="LONG: TP must be above entry"
        )
    if signal.direction == Direction.SHORT and signal.tp >= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="SHORT: TP must be below entry"
        )

    # R:R ratio (round to 2dp to avoid floating-point edge cases like 0.54/0.54 = 0.9999)
    risk = abs(signal.entry - signal.sl)
    reward = abs(signal.tp - signal.entry)
    if risk > 0:
        rr_ratio = round(reward / risk, 2)
        if rr_ratio < settings.min_rr:
            return ValidationResult(
                status=ValidationStatus.IGNORED,
                reason=f"R:R {rr_ratio:.2f} below minimum {settings.min_rr}",
            )

    # Minimum SL distance check
    if settings.min_sl_pct > 0:
        sl_pct = risk / signal.entry
        if sl_pct < settings.min_sl_pct:
            return ValidationResult(
                status=ValidationStatus.IGNORED,
                reason=f"SL distance {sl_pct:.4%} below minimum {settings.min_sl_pct:.4%}",
            )

    # Duplicate detection
    _cleanup_stale_entries()
    sig_key = (signal.symbol, signal.direction.value, signal.entry)
    if sig_key in _recent_signals:
        return ValidationResult(
            status=ValidationStatus.IGNORED,
            reason=f"Duplicate signal within {settings.duplicate_window_seconds}s",
        )
    _recent_signals[sig_key] = time.time()

    return ValidationResult(status=ValidationStatus.VALID)
