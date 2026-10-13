"""The gate matrix document, generated from the code.

`docs/GATE-MATRIX.md` answers, for every gate: what class of question it asks, whether it is hard,
which stage it belongs to, what "not evaluated" means for it, what a failure does to the research
verdict and to promotion, and which tests exercise it. It is generated so that it cannot drift from
`evaluation._GATES`, `gate_semantics` and `promotion`; tests/test_gate_matrix.py fails when stale.

Thresholds and their evidence tiers live in STRATEGY-ACCEPTANCE-CRITERIA.md and are not copied.

    uv run python -m signal_engine.backtest.gate_matrix --write
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from signal_engine.backtest import gate_semantics as gs
from signal_engine.backtest.promotion import (
    DIAGNOSTIC_GATES,
    PAPER_EXEMPT_GATES,
    STAGE_OVERRIDES,
    gate_order,
    stage_of,
)

DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "GATE-MATRIX.md"
TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"

#: Gates whose evaluator can only emit `report` (never pass or fail): shown, never blocking
#: when the input exists; the row is open only when the input is missing.
REPORT_ONLY = frozenset(
    "G0.6 G0.8 G1.4 G1.6 G2.6 G2.7 G3.1 G4.2 G4.3 G4.6 G4.7 G6.2 G6.3 G6.5".split()
)

#: Test files for gates whose tests are not named after the gate id. Every file is checked to exist.
EXTRA_TESTS: dict[str, tuple[str, ...]] = {
    "G0.2": ("test_truncation.py", "test_validity_trials_integration.py"),
    **dict.fromkeys(
        ["G0.3", "G0.4", "G0.6", "G0.8", "G4.7", "G6.2"], ("test_data_session_integration.py",)
    ),
    **dict.fromkeys(["G0.5", "G1.6", "G6.1", "G6.5"], ("test_evidence_ledgers.py",)),
    **dict.fromkeys(["G1.1", "G1.3"], ("test_evaluation_stats_gates.py",)),
    **dict.fromkeys(
        [
            "G1.2",
            "G1.4",
            "G1.5",
            "G2.1",
            "G2.2",
            "G3.1",
            "G3.2",
            "G3.4",
            "G4.4",
            "G4.5",
            "G6.9",
            "X.1",
            "X.2",
            "X.3",
            "X.4",
        ],
        ("test_evaluation.py",),
    ),
    **dict.fromkeys(["G2.3", "G2.6", "G2.7", "G3.3", "G3.5"], ("test_evaluation_stats_gates.py",)),
    "G2.4": ("test_evaluation_stats_gates.py", "test_verdict_mvp_policy.py"),
    "G2.5": ("test_validity_trials_integration.py",),
    "G4.1": ("test_perturbation.py",),
    "G6.4": ("test_tickgrid.py", "test_tickgrid_engine.py"),
    **dict.fromkeys(["G4.8", "G6.6"], ("test_verdict_mvp_policy.py",)),
}
#: Tests that cover every required gate at once.
ALL_GATES_TESTS = ("test_verdict_mvp_policy.py", "test_gate_semantics.py", "test_promotion.py")


def _tests(gate: str) -> list[str]:
    key = gate.lower().replace(".", "")
    named = sorted(p.name for p in TESTS_DIR.glob(f"test_{key}_*.py"))
    return sorted({*named, *EXTRA_TESTS.get(gate, ())})


def effect_for(gate: str) -> dict[str, Any]:
    """The verdict effect, promotion effect and tests of one gate, as plain text."""
    from signal_engine.backtest import evaluation as ev

    meta = {g[0]: g for g in ev._GATES}[gate]
    hard = bool(meta[3])
    cls = gs.GATE_CLASS[gate]
    stage = stage_of(gate)
    not_ready = "fail or not evaluated: backtest stage NOT_READY"
    if gate in DIAGNOSTIC_GATES:
        required_for = "optional diagnostic: blocks nothing"
    elif gate in PAPER_EXEMPT_GATES:
        required_for = "live only (pre-live requirement): " + PAPER_EXEMPT_GATES[gate].split(":")[0]
    elif stage == "backtest":
        required_for = "paper entry (and the backtest verdict)"
    elif stage == "paper":
        required_for = "paper stage (before live)"
    else:
        required_for = "live stage"
    if gate in DIAGNOSTIC_GATES:
        verdict = "none (diagnostic: shown, never decides)"
        promotion = "none (diagnostic)"
    elif stage != "backtest":
        verdict = (
            f"none for the backtest verdict; disclosed as required before the {stage} stage"
            if gate in STAGE_OVERRIDES
            else f"none (checked at the {stage} stage)"
        )
        promotion = f"fail or not evaluated: blocks the {stage} stage"
    elif gate == "G2.1":
        verdict = "fail or not evaluated: INCONCLUSIVE (too little evidence to judge)"
        promotion = not_ready
    elif cls == gs.INTEGRITY:
        verdict = (
            "fail: INCONCLUSIVE (the simulation cannot be trusted); not evaluated: held at MARGINAL"
        )
        promotion = not_ready
    elif hard:
        verdict = "fail: NO_EDGE; not evaluated: held at MARGINAL"
        promotion = not_ready
    else:
        verdict = "fail or not evaluated: held at MARGINAL"
        promotion = not_ready
    if gate in REPORT_ONLY:
        emits = "report only (cannot pass or fail)"
    elif gate not in ev._EVALUATORS:
        emits = "no evaluator (always not evaluated)"
    else:
        emits = "decides"
    cause = gs.NOT_EVALUATED_CAUSE[gate]
    return {
        "gate": gate,
        "title": meta[2],
        "class": cls,
        "hard": hard,
        "stage": stage,
        "emits": emits,
        "cause": cause,
        "not_evaluated_means": gs.INCONCLUSIVE if cause == gs.SAMPLE else gs.BLOCKED,
        "required_for": required_for,
        "verdict_effect": verdict,
        "promotion_effect": promotion,
        "tests": _tests(gate),
    }


def rows() -> list[dict[str, Any]]:
    """One row per gate, in gate order."""
    from signal_engine.backtest import evaluation as ev

    return [effect_for(g) for g in sorted((g[0] for g in ev._GATES), key=gate_order)]


_HEADER = """# Gate matrix

Generated by `signal_engine/backtest/gate_matrix.py` from the gate table, `gate_semantics.py` and
`promotion.py`. Do not edit by hand: change the code and run
`uv run python -m signal_engine.backtest.gate_matrix --write`. `tests/test_gate_matrix.py` fails when
this file is stale. Thresholds, evidence tiers and rationale are in
`STRATEGY-ACCEPTANCE-CRITERIA.md`; this file does not copy them.

## Three questions, kept apart

| Question | Answered by | Possible answers |
| --- | --- | --- |
| Research validity: can the data and the simulation support any conclusion? | integrity gates, the validity block, data quality | PASS, FAIL, BLOCKED |
| Strategy evidence: is there a credible edge after costs? | strategy-quality and risk gates | EDGE, PROMISING, MARGINAL, NO_EDGE, INCONCLUSIVE |
| Promotion readiness: may the strategy move to the next stage? | stage decision, limitations, dirty tree, reproducibility | READY_FOR_NEXT_STAGE, NOT_READY, BLOCKED |

A missing prerequisite is BLOCKED and too little data is INCONCLUSIVE; neither is a failure of the
strategy and neither is ever a pass. A failed integrity gate makes the verdict INCONCLUSIVE, because
the simulation, not the strategy, is what failed.

## Gate classes

"""

_COLUMNS = (
    "Gate",
    "Requirement",
    "Class",
    "Hard",
    "Stage",
    "Evaluator",
    "Required for",
    "Not evaluated means",
    "Verdict effect",
    "Promotion effect",
    "Tests",
)


def render() -> str:
    """The whole document as text."""
    out = [_HEADER]
    out += [f"- **{c}**: {gs.CLASS_TEXT[c]}" for c in gs.CLASSES]
    out += ["", '## Why a gate reads "not evaluated"', ""]
    out += [f"- **{c}**: {gs.CAUSE_TEXT[c]}" for c in gs.CAUSES]
    out += ["", "## Matrix", "", "| " + " | ".join(_COLUMNS) + " |"]
    out.append("| " + " | ".join("---" for _ in _COLUMNS) + " |")
    for r in rows():
        tests = ", ".join(f"`{t}`" for t in r["tests"]) or "none (no evaluator)"
        cells = (
            r["gate"],
            r["title"],
            r["class"],
            "yes" if r["hard"] else "no",
            r["stage"] or "-",
            r["emits"],
            r["required_for"],
            f"{r['not_evaluated_means']} ({r['cause']})",
            r["verdict_effect"],
            r["promotion_effect"],
            tests,
        )
        out.append("| " + " | ".join(cells) + " |")
    out += [
        "",
        "Every required gate is also covered as a group by "
        + ", ".join(f"`{t}`" for t in ALL_GATES_TESTS)
        + ".",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = render()
    if "--write" in args:
        DOC_PATH.write_text(text, encoding="utf-8")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
