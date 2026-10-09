"""Derived run verdict (lifecycle 1.5, decision U4). Pure, no I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from signal_engine.backtest.promotion import (
    DIAGNOSTIC_GATES,
    STAGE_OVERRIDES,
    evaluation_rows,
    stage_of,
    status_label,
)

EDGE = "EDGE"
MARGINAL = "MARGINAL"
NO_EDGE = "NO_EDGE"
INCONCLUSIVE = "INCONCLUSIVE"

#: Minimum-sample gates that decide the verdict (rule 2). The acceptance criteria name two rows
#: about sample size: G2.1 (trade count, with a 30-trade floor per reported subgroup) is a
#: requirement; G2.7 (track length against the Minimum Track Record Length) is labelled REPORT,
#: so it is shown as a diagnostic and never decides anything.
MIN_SAMPLE_GATES = ("G2.1",)
MIN_SAMPLE_DIAGNOSTIC_GATES = DIAGNOSTIC_GATES
_NOT_REQUIRED = frozenset({"report", "not_applicable"})


def _reason(text: str, gate: str | None = None, status: str | None = None) -> dict[str, Any]:
    return {
        "text": text,
        "gate": gate,
        "status": status,
        "evidence_ref": f"evaluation:{gate}" if gate else None,
    }


def _gate_reason(row: Mapping[str, Any], why: str) -> dict[str, Any]:
    gate = str(row["gate"])
    title = row.get("title") or gate
    label = status_label(str(row.get("status", "")))
    verb = "failed" if label == "FAIL" else "was not run"
    return _reason(f"{why}: {title} ({gate}) {verb}.", gate, label)


def _result(
    verdict: str,
    reasons: list[dict[str, Any]],
    primary: str | None = None,
    caps: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "run_verdict": verdict,
        "primary_failed_gate": primary,
        "reasons": reasons,
        "caps_applied": caps or [],
    }


def _derive(
    evaluation: Mapping[str, Any] | None,
    *,
    validity: Mapping[str, Any],
    data_quality: Mapping[str, Any],
    readiness_missing: Sequence[str],
    code_dirty: bool = False,
    reproducible: bool = True,
) -> dict[str, Any]:
    """Apply the verdict rules in order; the first match decides.

    `code_dirty` and `reproducible` are accepted so callers pass the whole run state, but they
    never change the verdict: they only affect the promotion decision.
    """
    if not validity.get("ok", True):
        reasons = [
            _reason(
                f"The run is not valid ({f.get('check')}): {f.get('detail')}. "
                "No performance result can be trusted."
            )
            for f in validity.get("failures") or []
        ] or [_reason("The run is not valid, so no performance result can be trusted.")]
        return _result(INCONCLUSIVE, reasons)
    if data_quality.get("status") == "WARNING" and data_quality.get("affects_validity"):
        items = ", ".join(str(i) for i in data_quality.get("items") or []) or "see data quality"
        return _result(
            INCONCLUSIVE,
            [_reason(f"Data quality problem that affects validity: {items}.")],
        )

    rows = evaluation_rows(evaluation)
    by_gate = {str(r["gate"]): r for r in rows}

    sample = [
        by_gate[g]
        if g in by_gate
        else {"gate": g, "title": "Trade count", "status": "not_evaluated"}
        for g in MIN_SAMPLE_GATES
    ]
    short = [r for r in sample if r["status"] in ("fail", "not_evaluated")]
    if short:
        return _result(
            INCONCLUSIVE,
            [_gate_reason(r, "Too few trades to judge") for r in short],
            str(short[0]["gate"]),
        )

    hard = [r for r in rows if r.get("hard") and r.get("status") == "fail"]
    if hard:
        return _result(
            NO_EDGE,
            [_gate_reason(r, "A hard requirement was not met") for r in hard],
            str(hard[0]["gate"]),
        )

    open_gates = [
        r
        for r in rows
        if stage_of(str(r["gate"])) == "backtest"
        and r.get("status") not in _NOT_REQUIRED
        and str(r["gate"]) not in DIAGNOSTIC_GATES
        and r.get("status") in ("fail", "not_evaluated")
    ]
    if open_gates:
        return _result(
            MARGINAL,
            [_gate_reason(r, "A backtest requirement is open") for r in open_gates],
            str(open_gates[0]["gate"]),
        )

    missing = [str(m) for m in readiness_missing]
    if missing:
        names = ", ".join(missing)
        return _result(
            MARGINAL,
            [
                _reason(
                    f"Engine readiness: the engine cannot yet verify {names}, so a clean result is held at marginal."
                )
            ],
            None,
            [f"engine readiness: {names}"],
        )
    return _result(EDGE, [])


def _diagnostics(evaluation: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    rows = {
        str(r["gate"]): r
        for dim in (evaluation or {}).get("dimensions", [])
        for r in dim.get("rows", [])
    }
    found = []
    for gate in MIN_SAMPLE_DIAGNOSTIC_GATES:
        row = rows.get(gate)
        if row is not None and row.get("status") in ("fail", "not_evaluated"):
            label = status_label(str(row["status"]))
            verb = "failed" if label == "FAIL" else "was not run"
            title = row.get("title") or gate
            found.append(
                _reason(
                    f"Diagnostic, does not decide the verdict: {title} ({gate}) {verb}.",
                    gate,
                    label,
                )
            )
    return found


def _stage_exceptions(evaluation: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Disclose gates checked at a later stage that have not been run, so a verdict never reads
    as full validation. They never change the verdict; promotion to their stage stays blocked."""
    rows = {str(r["gate"]): r for r in evaluation_rows(evaluation)}
    found = []
    for gate, (stage, why) in STAGE_OVERRIDES.items():
        row = rows.get(gate)
        if row is not None and row.get("status") in ("fail", "not_evaluated"):
            label = status_label(str(row["status"]))
            verb = "failed" if label == "FAIL" else "was not run"
            title = row.get("title") or gate
            found.append(
                _reason(
                    f"Required before the {stage} stage, does not decide this verdict: "
                    f"{title} ({gate}) {verb}; {why}.",
                    gate,
                    label,
                )
            )
    return found


def derive(
    evaluation: Mapping[str, Any] | None,
    *,
    validity: Mapping[str, Any],
    data_quality: Mapping[str, Any],
    readiness_missing: Sequence[str],
    code_dirty: bool = False,
    reproducible: bool = True,
) -> dict[str, Any]:
    """Apply the verdict rules in order; the first match decides (see `_derive`).

    Diagnostic sample-size rows (`MIN_SAMPLE_DIAGNOSTIC_GATES`) are appended to the reasons for
    a run whose performance gates were evaluated. They never change the verdict.
    """
    result = _derive(
        evaluation,
        validity=validity,
        data_quality=data_quality,
        readiness_missing=readiness_missing,
        code_dirty=code_dirty,
        reproducible=reproducible,
    )
    performance_evaluated = validity.get("ok", True) and not (
        data_quality.get("status") == "WARNING" and data_quality.get("affects_validity")
    )
    if performance_evaluated:
        extra = [*_diagnostics(evaluation), *_stage_exceptions(evaluation)]
        result = {**result, "reasons": [*result["reasons"], *extra]}
    return result
