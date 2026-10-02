"""Per-stage promotion decision for a finished run (lifecycle 1.6). Pure, no I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

STAGES = ("backtest", "paper", "live")
STAGE_GATES: dict[str, range] = {
    "backtest": range(0, 7),
    "paper": range(7, 9),
    "live": range(9, 10),
}
READY = "READY_FOR_NEXT_STAGE"
NOT_READY = "NOT_READY"
BLOCKED = "BLOCKED"

_STATUS = {
    "pass": "PASS",
    "fail": "FAIL",
    "not_evaluated": "NOT_RUN",
    "not_applicable": "NOT_APPLICABLE",
    "report": "REPORT",
}
_NOT_REQUIRED = frozenset({"NOT_APPLICABLE", "REPORT"})
_GATE_RE = re.compile(r"^G(\d+)\.(\d+)$")


def gate_number(gate: str) -> int | None:
    """Stage number of a gate id such as G3.2, or None for ids outside G<n>.<m>."""
    m = _GATE_RE.match(gate)
    return int(m.group(1)) if m else None


def gate_order(gate: str) -> tuple[int, int, str]:
    """Sort key putting G<n>.<m> ids in numeric order and any other id last."""
    m = _GATE_RE.match(gate)
    if m:
        return (int(m.group(1)), int(m.group(2)), gate)
    return (10_000, 0, gate)


def status_label(status: str) -> str:
    """Map an evaluation status to the promotion vocabulary."""
    return _STATUS.get(status, "NOT_RUN")


def evaluation_rows(evaluation: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """All rows of an evaluation block, in gate order. Tolerates missing blocks."""
    rows = [
        row for dim in (evaluation or {}).get("dimensions") or [] for row in dim.get("rows") or []
    ]
    return sorted(rows, key=lambda r: gate_order(str(r.get("gate", ""))))


def stage_of(gate: str) -> str | None:
    n = gate_number(gate)
    if n is None:
        return None
    return next((s for s, r in STAGE_GATES.items() if n in r), None)


def _stage_rows(rows: list[dict[str, Any]], stage: str) -> list[dict[str, str]]:
    return [
        {"gate": r["gate"], "status": status_label(r.get("status", ""))}
        for r in rows
        if (stage_of(str(r.get("gate", ""))) or "backtest") == stage
    ]


def evaluate_promotion(
    evaluation: Mapping[str, Any] | None,
    *,
    code_dirty: bool,
    reproducible: bool,
    readiness_missing: Sequence[str] = (),
) -> dict[str, Any]:
    """Decide READY_FOR_NEXT_STAGE, NOT_READY or BLOCKED for each stage.

    Rows whose gate id is outside G0..G9 count toward the backtest stage. Engine capabilities
    listed in `readiness_missing` keep the backtest stage NOT_READY.
    """
    rows = evaluation_rows(evaluation)
    stages: dict[str, dict[str, Any]] = {}
    blocker: str | None = None
    for stage in STAGES:
        gates = _stage_rows(rows, stage)
        if blocker is not None:
            stages[stage] = {
                "decision": BLOCKED,
                "gates": gates,
                "reasons": [f"the {blocker} stage is not ready"],
            }
            continue
        required = [g for g in gates if g["status"] not in _NOT_REQUIRED]
        reasons: list[str] = []
        if not required:
            reasons.append("no gates evaluated for this stage")
        for g in required:
            if g["status"] == "FAIL":
                reasons.append(f"gate {g['gate']} failed")
            elif g["status"] == "NOT_RUN":
                reasons.append(f"gate {g['gate']} has not been run")
        if stage == "backtest" and readiness_missing:
            reasons.append("engine readiness: " + ", ".join(str(m) for m in readiness_missing))
        if code_dirty:
            reasons.append("dirty working tree")
        if not reproducible:
            reasons.append("not reproducible")
        if reasons:
            blocker = stage
            decision = NOT_READY
        else:
            decision = READY
        stages[stage] = {"decision": decision, "gates": gates, "reasons": reasons}
    next_stage = None
    for prev, nxt in zip(STAGES, STAGES[1:], strict=False):
        if stages[prev]["decision"] == READY and stages[nxt]["decision"] != READY:
            next_stage = nxt
            break
    return {"stages": stages, "next_stage": next_stage}
