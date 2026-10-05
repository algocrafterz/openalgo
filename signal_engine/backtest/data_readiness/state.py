"""The data-readiness state machine (plan section 5.2, owner decisions D15, D16, D19).

Three states only. NOT_READY: the backtest cannot start. READY_WITH_WARNINGS: it may run and the
warnings are persisted. READY: it runs normally. Order of evaluation: any block, then the
defect-exclusion thresholds (count OR share, whichever is reached first) and the minimum number of
eligible symbols, then warnings or exclusions, then READY.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from signal_engine.backtest.data_readiness.policy import Policy

NOT_READY = "NOT_READY"
READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
READY = "READY"
STATES = (NOT_READY, READY_WITH_WARNINGS, READY)

DEFECT = "defect"
ELIGIBILITY = "eligibility"


@dataclass(frozen=True)
class Exclusion:
    """One excluded symbol (session None) or one excluded symbol-session, with its reason."""

    symbol: str
    session: Any
    rule_id: str
    dimension: str
    cls: str


@dataclass(frozen=True)
class Assessment:
    state: str
    reasons: tuple[str, ...]
    requested: int
    eligible: int
    defect_excluded_symbols: int
    eligibility_excluded_symbols: int
    excluded_sessions: int


def _over_share(exclusions: list[Exclusion], sessions_per_symbol: Mapping[str, int] | None,
                max_session_share: float | None) -> set[str]:
    """Symbols whose excluded sessions exceed the share of their sessions."""
    if sessions_per_symbol is None or max_session_share is None:
        return set()
    lost: dict[str, set] = {}
    for ex in exclusions:
        if ex.session is not None:
            lost.setdefault(ex.symbol, set()).add(ex.session)
    return {s for s, days in lost.items()
            if len(days) > max_session_share * sessions_per_symbol.get(s, 0)}


def apply_session_policy(exclusions: Iterable[Exclusion], session_exclusion_allowed: bool,
                         sessions_per_symbol: Mapping[str, int] | None = None,
                         max_session_share: float | None = None) -> list[Exclusion]:
    """Escalate session exclusions to whole-symbol exclusions, keeping one entry per symbol and
    rule. A session becomes a symbol exclusion when the contract forbids dropping single sessions
    (D16), or when the symbol would lose more than `max_session_share` of its sessions."""
    items = list(exclusions)
    escalate = _over_share(items, sessions_per_symbol, max_session_share)
    out: list[Exclusion] = []
    seen: set[tuple[str, str]] = set()
    for ex in items:
        if ex.session is not None and (not session_exclusion_allowed or ex.symbol in escalate):
            ex = Exclusion(ex.symbol, None, ex.rule_id, ex.dimension, ex.cls)
        if ex.session is None:
            key = (ex.symbol, ex.rule_id)
            if key in seen:
                continue
            seen.add(key)
        out.append(ex)
    return out


def _excluded_symbols(exclusions: Sequence[Exclusion], cls: str) -> set[str]:
    return {e.symbol for e in exclusions if e.session is None and e.cls == cls}


def assess(*, requested: int, blocks: Sequence[str], warnings: Sequence[str],
           exclusions: Sequence[Exclusion], eligible: int, min_symbols: int,
           policy: Policy, require_ready: bool = False) -> Assessment:
    """Compute the readiness state. `blocks` and `warnings` are rule ids (or named reasons)."""
    defect = _excluded_symbols(exclusions, DEFECT)
    elig = _excluded_symbols(exclusions, ELIGIBILITY) - defect
    sessions = sum(1 for e in exclusions if e.session is not None)
    counts = (requested, eligible, len(defect), len(elig), sessions)

    if blocks:
        return Assessment(NOT_READY, tuple(blocks), *counts)
    reasons: list[str] = []
    if requested <= 0:
        reasons.append("no_requested_symbols")
    if len(defect) > policy.max_defect_excluded_symbols:
        reasons.append("defect_exclusion_count")
    if requested and len(defect) / requested > policy.max_defect_excluded_symbol_share:
        reasons.append("defect_exclusion_share")
    if eligible < min_symbols:
        reasons.append("COV-MIN-SYMBOLS")
    if reasons:
        return Assessment(NOT_READY, tuple(reasons), *counts)
    if warnings or exclusions:
        if require_ready:
            return Assessment(NOT_READY, ("require_ready",), *counts)
        return Assessment(READY_WITH_WARNINGS, tuple(warnings), *counts)
    return Assessment(READY, (), *counts)
