"""Signal validation against trading rules and risk constraints."""

import time
from typing import Dict, Tuple


from signal_engine.config import settings
from signal_engine.models import Direction, Signal, ValidationResult, ValidationStatus

# In-memory duplicate tracker: (symbol, direction, entry) -> timestamp
_recent_signals: Dict[Tuple[str, str, object], float] = {}


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

    Checks run in order; the first failure wins. EXIT signals take a short path —
    TP HIT alerts synthesize Entry: 0.0, so SL/TP/R:R checks do not apply to them.
    """
    for check in _CHECKS:
        result = check(signal)
        if result is not None:
            return result
    return ValidationResult(status=ValidationStatus.VALID)


def _check_blacklist(signal: Signal):
    """Symbol blacklist — global then strategy-specific.

    Runs before the EXIT short path is taken, but skips EXIT signals: an open
    position must still be closable even if its symbol was blacklisted since entry.
    """
    if signal.direction == Direction.EXIT:
        return None
    symbol_upper = signal.symbol.upper()
    if symbol_upper in settings.blacklist.get("_GLOBAL", frozenset()):
        return ValidationResult(
            status=ValidationStatus.IGNORED,
            reason=f"{signal.symbol} is blacklisted (global)",
        )
    if symbol_upper in settings.blacklist.get(signal.strategy.upper(), frozenset()):
        return ValidationResult(
            status=ValidationStatus.IGNORED,
            reason=f"{signal.symbol} is blacklisted for {signal.strategy}",
        )
    return None


def _check_exit_shortpath(signal: Signal):
    """EXIT signals need only a symbol — they close, they do not open.

    Levelled exits (TP1/TP1.5/SL) still go through dedup on the way out: PineScript emits
    each level exactly once per trade, so a repeat is a delivery artefact rather than a
    second instruction. A bare EXIT carries no level and stays retryable — a manual or
    safety close must always be able to fire again after a failed attempt.
    """
    if signal.direction != Direction.EXIT:
        return None
    if not signal.symbol or signal.symbol.strip() == "":
        return ValidationResult(status=ValidationStatus.INVALID, reason="EXIT: symbol required")
    if signal.tp_level:
        return _check_duplicate(signal) or ValidationResult(status=ValidationStatus.VALID)
    return ValidationResult(status=ValidationStatus.VALID)


def _check_prices_positive(signal: Signal):
    """Entry, SL and TP must all be real prices."""
    if signal.entry <= 0:
        return ValidationResult(status=ValidationStatus.INVALID, reason="Entry must be positive")
    if signal.sl <= 0:
        return ValidationResult(status=ValidationStatus.INVALID, reason="SL must be positive")
    if signal.tp <= 0:
        return ValidationResult(status=ValidationStatus.INVALID, reason="TP must be positive")
    return None


def _check_price_ordering(signal: Signal):
    """SL and TP must sit on the correct side of entry for the trade direction."""
    if signal.direction == Direction.LONG and signal.sl >= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="LONG: SL must be below entry"
        )
    if signal.direction == Direction.SHORT and signal.sl <= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="SHORT: SL must be above entry"
        )
    if signal.direction == Direction.LONG and signal.tp <= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="LONG: TP must be above entry"
        )
    if signal.direction == Direction.SHORT and signal.tp >= signal.entry:
        return ValidationResult(
            status=ValidationStatus.INVALID, reason="SHORT: TP must be below entry"
        )
    return None


def _check_reward_risk(signal: Signal):
    """Reward:risk must clear the configured floor.

    Rounded to 2dp to avoid floating-point edge cases like 0.54/0.54 = 0.9999.
    """
    risk = abs(signal.entry - signal.sl)
    if risk <= 0:
        return None
    rr_ratio = round(abs(signal.tp - signal.entry) / risk, 2)
    if rr_ratio < settings.min_rr:
        return ValidationResult(
            status=ValidationStatus.IGNORED,
            reason=f"R:R {rr_ratio:.2f} below minimum {settings.min_rr}",
        )
    return None


def _check_sl_distance(signal: Signal):
    """Reject stops so tight that slippage alone would trigger them."""
    floor = _min_sl_pct_for(signal.strategy)
    if floor <= 0:
        return None
    sl_pct = abs(signal.entry - signal.sl) / signal.entry
    if sl_pct < floor:
        return ValidationResult(
            status=ValidationStatus.IGNORED,
            reason=f"SL distance {sl_pct:.4%} below minimum {floor:.4%}",
        )
    return None


def _min_sl_pct_for(strategy: str) -> float:
    """The stop-distance floor for this strategy, falling back to the global one.

    Stop geometry is a property of the strategy, not of the account: an ORB stop sits at a
    fraction of the opening range while a key-level stop sits 0.35 ATR beyond the level,
    which is several times tighter. One global floor cannot serve both — the ORB-calibrated
    0.5% rejected all 7 BREAKOUT signals on 2026-08-24.
    """
    profile = settings.strategy_profiles.get(strategy.upper(), {})
    override = profile.get("min_sl_pct")
    return settings.min_sl_pct if override is None else float(override)


def _check_duplicate(signal: Signal):
    """Suppress a repeat of the same symbol/direction/entry inside the dedup window.

    For EXIT signals entry is always the synthesized 0.0, so the TP level stands in as the
    discriminator — otherwise TP1 and the TP1.5 that follows it would collide.
    """
    _cleanup_stale_entries()
    sig_key = (signal.symbol, signal.direction.value, signal.tp_level or signal.entry)
    if sig_key in _recent_signals:
        return ValidationResult(
            status=ValidationStatus.IGNORED,
            reason=f"Duplicate signal within {settings.duplicate_window_seconds}s",
        )
    _recent_signals[sig_key] = time.time()
    return None


# Order matters: blacklist before the EXIT short path, duplicate last (it records state).
_CHECKS = (
    _check_blacklist,
    _check_exit_shortpath,
    _check_prices_positive,
    _check_price_ordering,
    _check_reward_risk,
    _check_sl_distance,
    _check_duplicate,
)
