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
#: Rows the acceptance criteria label REPORT: shown, never required for promotion or the verdict.
#: G2.7 (track length against the Minimum Track Record Length), plus the X rows: heuristics
#: that sit outside the criteria document (parameter count, regime dispersion and activation).
#: One list serves the verdict, the promotion decision and the next action, so the three can
#: never disagree about whether a row is required.
DIAGNOSTIC_GATES: tuple[str, ...] = ("G2.7", "X.1", "X.2", "X.3", "X.4")
#: Gates assigned to a stage other than the stage of their number, each with its rationale.
#: Owner decision of 2026-10-09, recorded in STRATEGY-ACCEPTANCE-CRITERIA.md. A gate may be
#: listed here only if it is not HARD and sits at G4 or above (tests/test_verdict_mvp_policy.py
#: enforces that), so a check that could invalidate backtest results cannot be moved out of the
#: verdict. A moved gate still blocks promotion at its stage and is disclosed in the verdict.
STAGE_OVERRIDES: dict[str, tuple[str, str]] = {
    "G4.8": (
        "paper",
        "event-day analysis has not been performed: it needs an event calendar (results dates, "
        "monthly expiry, index rebalance, budget and election days) that is not held, so how "
        "much of the result depends on event days is unknown until it is evaluated",
    ),
    "G6.6": (
        "paper",
        "operational authorization (broker-registered API, static IP, 2FA, order tagging), "
        "not the validity of a backtest",
    ),
}
#: Gates that stay in the backtest verdict (a missing one still holds EDGE) but need not HAVE RUN
#: for the backtest-to-paper step. Each is a pre-live requirement: its absence does not bear on
#: whether the simulated edge is real, and paper trading is itself the check it stands in for.
#: They are required again at the live stage. A FAILED exempt gate still blocks paper: the
#: exemption is from running, never from passing. Owner-visible: changing this set changes who
#: reaches paper.
PAPER_EXEMPT_GATES: dict[str, str] = {
    "G0.7": (
        "test-coverage attestation: a process record about the code, not evidence about the "
        "edge; the live stage needs it"
    ),
    "G2.4": (
        "probability of backtest overfitting needs the daily P&L of every tried variant, which "
        "is not stored; multiple testing is already controlled by the trial-adjusted t-hurdle "
        "(G2.2) and the deflated Sharpe (G2.3), and paper trading is the out-of-sample test "
        "that overfitting would fail; the live stage needs it"
    ),
}
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
    """The stage a gate is checked at: its override if listed, else the stage of its number."""
    if gate in STAGE_OVERRIDES:
        return STAGE_OVERRIDES[gate][0]
    n = gate_number(gate)
    if n is None:
        return None
    return next((s for s, r in STAGE_GATES.items() if n in r), None)


def promotion_stage(gate: str) -> str:
    """The stage whose promotion this gate holds: live-only gates move to `live`."""
    if gate in PAPER_EXEMPT_GATES:
        return "live"
    return stage_of(gate) or "backtest"


def _stage_rows(rows: list[dict[str, Any]], stage: str) -> list[dict[str, str]]:
    return [
        {"gate": r["gate"], "status": status_label(r.get("status", ""))}
        for r in rows
        if promotion_stage(str(r.get("gate", ""))) == stage
    ]


def _complete_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows plus a not-evaluated row for every known gate the evaluation lacks.

    A missing row is a gate that did not run. Without this, an evaluation that simply omitted a
    failing gate would look cleaner than one that reported it.
    """
    from signal_engine.backtest.gate_semantics import GATE_CLASS

    present = {str(r.get("gate")) for r in rows}
    extra = [{"gate": g, "status": "not_evaluated"} for g in GATE_CLASS if g not in present]
    return sorted([*rows, *extra], key=lambda r: gate_order(str(r.get("gate", ""))))


def _invalid_reasons(
    validity: Mapping[str, Any] | None,
    data_quality: Mapping[str, Any] | None,
    run_status: str | None,
) -> list[str]:
    """Why the run cannot be trusted at all. Missing inputs count against the run."""
    reasons: list[str] = []
    if validity is None:
        reasons.append("validity was not supplied")
    else:
        reasons += [
            f"run is not valid ({f.get('check')}): {f.get('detail')}"
            for f in validity.get("failures") or []
        ]
        if not validity.get("ok", False) and not reasons:
            reasons.append("run is not valid")
    if data_quality is None:
        reasons.append("data quality was not supplied")
    elif data_quality.get("status") == "WARNING" and data_quality.get("affects_validity"):
        items = ", ".join(str(i) for i in data_quality.get("items") or []) or "see data quality"
        reasons.append(f"data quality problem that affects validity: {items}")
    if run_status != "VALID":
        reasons.append(f"run status is {run_status!r}, not 'VALID'")
    return reasons


def evaluate_promotion(
    evaluation: Mapping[str, Any] | None,
    *,
    code_dirty: bool,
    reproducible: bool,
    validity: Mapping[str, Any] | None,
    data_quality: Mapping[str, Any] | None,
    run_status: str | None,
    readiness_missing: Sequence[str] = (),
    readiness_critical: Sequence[str] = (),
) -> dict[str, Any]:
    """Decide READY_FOR_NEXT_STAGE, NOT_READY or BLOCKED for each stage. Fails closed.

    `validity`, `data_quality` and `run_status` are required with no default and may not be
    None: a caller cannot build an input that skips them. A run whose validity block failed,
    whose data quality affects validity, or whose status is not 'VALID' is never promotable. The
    research verdict is re-derived here from the same inputs, and a NO_EDGE or INCONCLUSIVE
    verdict is never promotable, whatever the stage rows say. A gate missing from the
    evaluation counts as not run.

    The backtest-to-paper step needs every required backtest gate to pass (so a candidate
    reaches paper on promising evidence without first being EDGE), except the diagnostic rows
    and the live-only gates in `PAPER_EXEMPT_GATES`, which are required at the live stage.

    Readiness: `readiness_critical` are unverified engine capabilities that bear on the
    trustworthiness of results (data handling, execution, costs, risk controls); they hold the
    paper step. The other entries of `readiness_missing` are scoped research limitations (for
    example a survivor-biased universe): they hold no stage and never cap the verdict; they are
    listed under `limitations` so no consumer reads a clean promotion as an unqualified claim.
    """
    from signal_engine.backtest.verdict import NO_EDGE, derive

    raw_rows = evaluation_rows(evaluation)
    rows = _complete_rows(raw_rows) if raw_rows else []
    critical = [str(m) for m in readiness_critical]
    limitations = [str(m) for m in readiness_missing if str(m) not in critical]
    invalid_reasons = _invalid_reasons(validity, data_quality, run_status)
    verdict = derive(
        evaluation,
        validity=validity if validity is not None else {"ok": False, "failures": []},
        data_quality=data_quality if data_quality is not None else {},
        readiness_missing=critical,
    )["run_verdict"]
    disqualified = (
        [f"research verdict is {verdict}"] if verdict in (NO_EDGE, "INCONCLUSIVE") else []
    )
    stages: dict[str, dict[str, Any]] = {}
    blocker: str | None = None
    for stage in STAGES:
        gates = _stage_rows(rows, stage)
        if blocker is not None:
            blocked_reasons = [f"the {blocker} stage is not ready"]
            stages[stage] = {"decision": BLOCKED, "gates": gates, "reasons": blocked_reasons}
            continue
        required = [
            g
            for g in gates
            if g["status"] not in _NOT_REQUIRED and g["gate"] not in DIAGNOSTIC_GATES
        ]
        reasons: list[str] = []
        if not raw_rows:
            if stage == "backtest":
                reasons.append("no evaluation supplied")
        elif not required and stage != "live":
            reasons.append("no gates evaluated for this stage")
        for g in required:
            if g["status"] == "FAIL":
                reasons.append(f"gate {g['gate']} failed")
            elif g["status"] == "NOT_RUN":
                reasons.append(f"gate {g['gate']} has not been run")
        if stage == "backtest":
            reasons.extend(
                f"gate {r['gate']} failed"
                for r in rows
                if r["gate"] in PAPER_EXEMPT_GATES and r.get("status") == "fail"
            )
            reasons.extend(invalid_reasons)
            reasons.extend(disqualified)
            if critical:
                reasons.append(
                    "critical engine capability not verified: " + ", ".join(critical)
                )
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
    return {
        "stages": stages,
        "next_stage": next_stage,
        "limitations": limitations,
        "critical": critical,
    }
