"""Gate semantics: what each gate is, what "not evaluated" means for it, and the three questions.

A gate result answers one of three different questions, and mixing them is how a missing
attestation gets read as a bad strategy or a bad strategy as a broken engine:

1. Research validity: is the data and the simulation trustworthy enough to support any conclusion?
2. Strategy evidence: given a trustworthy test, is there a credible edge after costs?
3. Promotion readiness: may the strategy move to the next stage (paper, then live)?

Stored gate statuses (`pass`, `fail`, `report`, `not_evaluated`, `not_applicable`) are unchanged,
so existing run records stay valid. This module derives the outcome vocabulary on top of them:
PASS, FAIL, BLOCKED, INCONCLUSIVE, NOT_APPLICABLE and REPORT. Pure functions, no I/O.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from signal_engine.backtest.promotion import (
    DIAGNOSTIC_GATES,
    PAPER_EXEMPT_GATES,
    evaluation_rows,
    gate_order,
    stage_of,
)

INTEGRITY = "integrity"
STRATEGY = "strategy_quality"
RISK = "risk_constraint"
OPERATIONAL = "promotion_operational"
CLASSES = (INTEGRITY, STRATEGY, RISK, OPERATIONAL)

CLASS_TEXT = {
    INTEGRITY: "Research-integrity prerequisite: data, causality or execution assumptions. "
    "A failure invalidates every conclusion that depends on it.",
    STRATEGY: "Strategy-quality test: edge, significance, out-of-sample stability, robustness.",
    RISK: "Risk constraint: drawdown, ruin, sizing, capacity and margin at the deployed size.",
    OPERATIONAL: "Promotion or operational requirement: checked at the paper or live stage.",
}

STAGE_NOT_RUN = "stage_not_run"
ATTESTATION = "attestation"
SAMPLE = "sample"
STRUCTURAL = "structural"
DATA_PREREQUISITE = "data_prerequisite"
CAUSES = (STAGE_NOT_RUN, ATTESTATION, SAMPLE, STRUCTURAL, DATA_PREREQUISITE)

CAUSE_TEXT = {
    STAGE_NOT_RUN: "BLOCKED: the opt-in evidence stage has not been run (or its evidence was "
    "rejected). Run the stage named in the next action.",
    ATTESTATION: "BLOCKED: no valid attestation covers this run. Record one for the exact commit.",
    SAMPLE: "INCONCLUSIVE: the evidence exists but is too small to decide (trade, window or "
    "symbol count below the rule's floor). More data, not a different strategy.",
    STRUCTURAL: "BLOCKED: cannot be resolved by a backtest. It needs a later stage, an external "
    "input or an owner decision.",
    DATA_PREREQUISITE: "BLOCKED: an input the gate reads is missing from the run record "
    "(often a record written before the input existed).",
}

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
INCONCLUSIVE = "INCONCLUSIVE"
NOT_APPLICABLE = "NOT_APPLICABLE"
REPORT = "REPORT"
OUTCOMES = (PASS, FAIL, BLOCKED, INCONCLUSIVE, NOT_APPLICABLE, REPORT)

_DECIDED = {"pass": PASS, "fail": FAIL, "not_applicable": NOT_APPLICABLE, "report": REPORT}

_INTEGRITY_GATES = (
    "G0.1 G0.2 G0.3 G0.4 G0.5 G0.6 G0.8 G1.1 G2.5 G3.5 G4.7 G6.1 G6.2 G6.3 G6.5 G6.7 G6.8 G6.9"
).split()
_RISK_GATES = "G5.1 G5.2 G5.3 G5.4 G5.5".split()
_STRATEGY_GATES = (
    "G1.2 G1.3 G1.4 G1.5 G1.6 G2.1 G2.2 G2.3 G2.4 G2.6 G2.7 G3.1 G3.2 G3.3 G3.4 "
    "G4.1 G4.2 G4.3 G4.4 G4.5 G4.6 G4.8 X.1 X.2 X.3 X.4"
).split()
_OPERATIONAL_GATES = (
    "G0.7 G6.4 G6.6 G7.1 G7.2 G7.3 G7.4 G7.5 G8.1 G8.2 G8.3 G8.4 G8.5 G8.6 G8.7 G8.8 "
    "G9.1 G9.2 G9.3 G9.4"
).split()

#: The class of every gate in `evaluation._GATES`. tests/test_gate_semantics.py keeps the two
#: in step, so a new gate cannot be added without being classified.
GATE_CLASS: dict[str, str] = {
    **dict.fromkeys(_INTEGRITY_GATES, INTEGRITY),
    **dict.fromkeys(_STRATEGY_GATES, STRATEGY),
    **dict.fromkeys(_RISK_GATES, RISK),
    **dict.fromkeys(_OPERATIONAL_GATES, OPERATIONAL),
}

#: The dominant reason a gate reads "not evaluated". A gate can have a secondary cause
#: (for example a rejected stage record); the evidence text of the row names it.
NOT_EVALUATED_CAUSE: dict[str, str] = {
    **dict.fromkeys(GATE_CLASS, DATA_PREREQUISITE),
    **dict.fromkeys("G0.2 G4.1 G4.2 G5.1 G5.2 G5.4 G5.5 G6.4".split(), STAGE_NOT_RUN),
    **dict.fromkeys(["G0.1", "G0.7"], ATTESTATION),
    **dict.fromkeys(
        "G1.2 G1.5 G2.1 G2.2 G2.3 G2.6 G2.7 G3.1 G3.2 G3.3 G3.4 G4.4 G4.5 G4.6 G5.3".split(),
        SAMPLE,
    ),
    **dict.fromkeys(["G2.4", "G4.3", "G4.8", *_OPERATIONAL_GATES], STRUCTURAL),
}


def gate_class(gate: str) -> str | None:
    """The class of a gate, or None for an id outside the gate table."""
    return GATE_CLASS.get(gate)


def outcome(row: Mapping[str, Any]) -> str:
    """The outcome of one evaluation row.

    A `not_evaluated` row is never a pass and never a fail. It is INCONCLUSIVE when the evidence
    exists but is too small, and BLOCKED when a prerequisite is missing. An unknown gate id with
    no result is BLOCKED.
    """
    status = str(row.get("status", ""))
    if status in _DECIDED:
        return _DECIDED[status]
    cause = NOT_EVALUATED_CAUSE.get(str(row.get("gate", "")), DATA_PREREQUISITE)
    return INCONCLUSIVE if cause == SAMPLE else BLOCKED


def _open_row(row: Mapping[str, Any]) -> dict[str, Any]:
    gate = str(row["gate"])
    out = outcome(row)
    item: dict[str, Any] = {
        "gate": gate,
        "title": row.get("title") or gate,
        "outcome": out,
        "class": gate_class(gate),
        "stage": stage_of(gate),
        "hard": bool(row.get("hard")),
    }
    if row.get("status") == "not_evaluated":
        item["cause"] = NOT_EVALUATED_CAUSE.get(gate, DATA_PREREQUISITE)
    return item


def _required(row: Mapping[str, Any]) -> bool:
    gate = str(row["gate"])
    return stage_of(gate) == "backtest" and gate not in DIAGNOSTIC_GATES


def _validity(
    rows: Sequence[Mapping[str, Any]],
    validity: Mapping[str, Any],
    data_quality: Mapping[str, Any],
    limitations: Sequence[str],
) -> dict[str, Any]:
    reasons: list[str] = []
    if not validity.get("ok", True):
        reasons += [
            f"run is not valid ({f.get('check')}): {f.get('detail')}"
            for f in validity.get("failures") or []
        ] or ["run is not valid"]
    if data_quality.get("status") == "WARNING" and data_quality.get("affects_validity"):
        reasons.append("data quality problem that affects validity")
    integrity = [r for r in rows if gate_class(str(r["gate"])) == INTEGRITY and _required(r)]
    failed = [r for r in integrity if outcome(r) == FAIL]
    open_ = [r for r in integrity if outcome(r) in (BLOCKED, INCONCLUSIVE)]
    reasons += [f"integrity gate {r['gate']} failed" for r in failed]
    if reasons:
        status = FAIL
    elif open_:
        status = BLOCKED
        reasons = [f"integrity gate {r['gate']} not evaluated" for r in open_]
    else:
        status = PASS
    return {"status": status, "reasons": reasons, "limitations": list(limitations)}


def _evidence(rows: Sequence[Mapping[str, Any]], verdict: Mapping[str, Any]) -> dict[str, Any]:
    legacy = str(verdict.get("run_verdict"))
    if legacy != "MARGINAL":
        return {"status": legacy, "reasons": [r.get("text") for r in verdict.get("reasons", [])]}
    # A strategy-quality or risk gate that is open for any reason holds the claim at MARGINAL.
    # A required gate of another class holds it only when it FAILED: an operational gate that
    # cannot run in a backtest (G6.4 part B, the G0.7 attestation) must not stop PROMISING.
    open_ = [
        r
        for r in rows
        if _required(r)
        and (
            (
                gate_class(str(r["gate"])) in (STRATEGY, RISK)
                and str(r["gate"]) not in PAPER_EXEMPT_GATES
                and outcome(r) in (FAIL, BLOCKED, INCONCLUSIVE)
            )
            or outcome(r) == FAIL
        )
    ]
    if open_:
        return {"status": "MARGINAL", "reasons": [f"{r['gate']} is {outcome(r)}" for r in open_]}
    return {
        "status": "PROMISING",
        "reasons": [
            "every strategy-quality and risk gate passes; the claim is held below EDGE only by "
            "an open integrity, operational or engine-readiness item named in the verdict"
        ],
    }


def assess(
    evaluation: Mapping[str, Any] | None,
    *,
    verdict: Mapping[str, Any],
    promotion: Mapping[str, Any],
    validity: Mapping[str, Any],
    data_quality: Mapping[str, Any],
) -> dict[str, Any]:
    """Answer the three questions for a run, from its own evaluation, verdict and promotion.

    `strategy_evidence` never contradicts the stored verdict: EDGE, NO_EDGE and INCONCLUSIVE
    pass through, and a MARGINAL verdict is refined to PROMISING only when every required
    strategy-quality and risk gate passes.
    """
    rows = evaluation_rows(evaluation)
    limitations = list(promotion.get("limitations") or [])
    stages = promotion.get("stages") or {}
    open_rows = [_open_row(r) for r in rows if outcome(r) in (FAIL, BLOCKED, INCONCLUSIVE)]
    counts = Counter(outcome(r) for r in rows)
    return {
        "research_validity": _validity(rows, validity, data_quality, limitations),
        "strategy_evidence": _evidence(rows, verdict),
        "promotion_readiness": {
            **{stage: (stages.get(stage) or {}).get("decision") for stage in stages},
            "next_stage": promotion.get("next_stage"),
        },
        "gates": {
            "counts": {o: counts.get(o, 0) for o in OUTCOMES},
            "open": open_rows,
        },
    }


def matrix_rows() -> list[dict[str, Any]]:
    """One row per gate, in gate order, for the gate matrix document."""
    from signal_engine.backtest import evaluation as ev

    rows = []
    for gate, dimension, title, hard, _why in ev._GATES:
        cause = NOT_EVALUATED_CAUSE[gate]
        rows.append(
            {
                "gate": gate,
                "title": title,
                "dimension": dimension,
                "class": GATE_CLASS[gate],
                "hard": hard,
                "stage": stage_of(gate),
                "evaluator": gate in ev._EVALUATORS,
                "not_evaluated_cause": cause,
                "not_evaluated_means": INCONCLUSIVE if cause == SAMPLE else BLOCKED,
            }
        )
    return sorted(rows, key=lambda r: gate_order(r["gate"]))
