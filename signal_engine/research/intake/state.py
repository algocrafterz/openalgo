"""Intake state: computed from the spec and the ledger, never stored.

A stored state could drift from the files it describes; a computed one cannot. The checks run
in this order and the first match decides:

  1. `blocked_reason` is set: BACKTEST_BLOCKED
  2. any blocker is unresolved: NEEDS_CLARIFICATION if there are open questions, else DRAFT
  3. any High entry that is not the author's own and not confirmed: ASSUMPTION_REVIEW
  4. otherwise BACKTEST_READY
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from signal_engine.research.intake import fields
from signal_engine.research.intake.ledger import Assumption, Ledger
from signal_engine.research.intake.spec import INTAKE, Problem, validate_spec

DRAFT = "DRAFT"
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
ASSUMPTION_REVIEW = "ASSUMPTION_REVIEW"
BACKTEST_READY = "BACKTEST_READY"
BACKTEST_BLOCKED = "BACKTEST_BLOCKED"
STATES = (DRAFT, NEEDS_CLARIFICATION, ASSUMPTION_REVIEW, BACKTEST_READY, BACKTEST_BLOCKED)


@dataclass(frozen=True)
class StateResult:
    """The state and the evidence for it."""

    state: str
    reasons: tuple[str, ...]
    blockers: tuple[Problem, ...]
    unconfirmed_high: tuple[Assumption, ...]
    unconfirmed_medium: tuple[Assumption, ...]


def _needs_confirmation(entry: Assumption, level: str) -> bool:
    return entry.materiality == level and entry.source != "user" and not entry.user_confirmed


def _resolved_by_ledger(problem: Problem, ledger: Ledger) -> bool:
    """A gap the platform can fill by inference or default is not a blocker once it has an entry."""
    if problem.kind != INTAKE or not ledger.has(problem.field):
        return False
    try:
        return fields.get(problem.field).cls in (fields.DERIVABLE, fields.DEFAULTABLE)
    except KeyError:
        return False


def intake_state(spec: Any, ledger: Ledger, profiles_dir: Path | None = None) -> StateResult:
    """The intake state of an idea, with the problems and entries that decide it."""
    blockers = tuple(
        p for p in validate_spec(spec, profiles_dir) if not _resolved_by_ledger(p, ledger)
    )
    high = tuple(e for e in ledger.entries if _needs_confirmation(e, fields.HIGH))
    medium = tuple(e for e in ledger.entries if _needs_confirmation(e, fields.MEDIUM))
    if ledger.blocked_reason:
        reasons = (f"blocked: {ledger.blocked_reason}",)
        return StateResult(BACKTEST_BLOCKED, reasons, blockers, high, medium)
    if blockers:
        found = NEEDS_CLARIFICATION if ledger.open_questions else DRAFT
        reasons = tuple(str(p) for p in blockers)
        return StateResult(found, reasons, blockers, high, medium)
    if high:
        reasons = tuple(f"{e.id} {e.field} = {e.value!r}: {e.reason}" for e in high)
        return StateResult(ASSUMPTION_REVIEW, reasons, blockers, high, medium)
    return StateResult(BACKTEST_READY, (), blockers, high, medium)
