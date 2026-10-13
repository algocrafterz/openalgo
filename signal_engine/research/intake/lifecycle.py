"""Idea lifecycle: stage, research state, the legacy fields derived from them, and stage evidence.

`stage` (inbox, backtest, paper, live, retired) and `research_state` are the only hand-written
lifecycle keys in `idea.md`. `status` and `verdict` survive only as fields the registry derives
for the Backtests page, REGISTRY.md and the site, which still read them. The rules for moving a
stage are in `docs/BACKTEST-LIFECYCLE-AND-RUN-SCHEMA.md` section 1.2.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

STAGES = ("inbox", "backtest", "paper", "live", "retired")
RESEARCH_STATES = (
    "active",
    "promising",
    "needs_more_evidence",
    "weak_evidence",
    "evidence_against_edge",
    "shelved",
    "rejected",
    "blocked",
)
DEFAULT_RESEARCH_STATE = "active"
#: States that block a stage move forward until they change. `blocked` is an untested idea that
#: cannot be run yet (its `block_reason` says why); it is not research evidence against it.
BLOCKING_STATES = ("rejected", "shelved", "blocked")
#: Stages whose entry needs a promoted run.
EVIDENCE_STAGES = ("paper", "live")

READY = "READY_FOR_NEXT_STAGE"
LIVE_BLOCKED = (
    "stage live cannot be reached yet: the paper to live gates (G7 and G8 gates are not "
    "implemented)"
)
LEGACY_KEYS = {
    "status": "status was split into stage and research_state",
    "verdict": "verdict is calculated per run (run_verdict) and is not a hand-written key",
}

_VERDICTS = {
    "EDGE": "edge",
    "MARGINAL": "marginal",
    "NO_EDGE": "no-edge",
    "INCONCLUSIVE": "inconclusive",
}
_STAGE_STATUS = {
    "inbox": "inbox",
    "paper": "paper",
    "live": "live",
    "retired": "shelved",
}


def derive_status(stage: str, research_state: str, has_runs: bool) -> str:
    """The legacy `status` for a stage and research state."""
    if research_state in BLOCKING_STATES:
        return research_state
    if stage == "backtest":
        return "backtested" if has_runs else "evaluating"
    return _STAGE_STATUS[stage]


def derive_verdict(latest_run: dict[str, Any] | None) -> str:
    """The legacy `verdict`: the latest run's run_verdict, or 'none'."""
    if not isinstance(latest_run, dict):
        return "none"
    return _VERDICTS.get(latest_run.get("run_verdict"), "none")


#: Run-record schemas whose flat fields this check understands. Schema 4 (2026-10-05) added
#: `run_status`; a schema 3 record has none and is judged on its other fields.
PROMOTABLE_SCHEMAS = (3, 4)


def run_blockers(run: dict[str, Any], version: str) -> list[str]:
    """Why `run` cannot promote `version`. An empty list means it qualifies."""
    blockers: list[str] = []
    schema = run.get("schema")
    if schema not in PROMOTABLE_SCHEMAS:
        blockers.append(f"has schema {schema!r}; promotable schemas are {PROMOTABLE_SCHEMAS}")
    # Defence in depth: the decision is derived from the same inputs, but a record whose flat
    # fields disagree with it must not promote. NO_EDGE and INCONCLUSIVE are never promotable.
    verdict = run.get("run_verdict")
    if verdict in ("NO_EDGE", "INCONCLUSIVE"):
        blockers.append(f"has run_verdict {verdict}, which is never promotable")
    if schema == 4 and run.get("run_status") != "VALID":
        blockers.append(f"has run_status {run.get('run_status')!r}, not 'VALID'")
    if run.get("reproducible") is not True:
        blockers.append("is not reproducible")
    if run.get("code_dirty") is not False:
        blockers.append("was made from a dirty working tree (code_dirty)")
    if run.get("strategy_version") != version:
        blockers.append(f"belongs to {run.get('strategy_version')}, not {version}")
    if run.get("promotion_decision") != READY:
        blockers.append(f"promotion_decision is {run.get('promotion_decision')!r}, not {READY}")
    return blockers


def find_promotable_run(
    runs: Sequence[dict[str, Any]], version: str, run_id: str | None = None
) -> tuple[dict[str, Any] | None, list[str]]:
    """The run that may promote `version`: the named one, else the latest qualifying one.

    Returns (run, []) or (None, reasons). `runs` is in date order, oldest first.
    """
    if run_id is not None:
        run = next((r for r in runs if r.get("run_id") == run_id), None)
        if run is None:
            return None, [f"run {run_id} is not a run of this idea"]
        blockers = run_blockers(run, version)
        return (None, [f"run {run_id} {b}" for b in blockers]) if blockers else (run, [])
    candidates = [r for r in runs if r.get("strategy_version") == version]
    if not candidates:
        return None, [f"no run of version {version} exists"]
    reasons: list[str] = []
    for run in reversed(candidates):
        blockers = run_blockers(run, version)
        if not blockers:
            return run, []
        reasons.extend(f"run {run.get('run_id')} {b}" for b in blockers)
    return None, reasons


def _is_blank(value: Any) -> bool:
    return value is None or (
        isinstance(value, (str, list)) and not any(str(v).strip() for v in value)
    )


def vocabulary_problems(idea: dict[str, Any]) -> list[str]:
    """Problems with the lifecycle keys alone: legacy keys, unknown values, shelved."""
    problems = [
        f"{key} is not a valid key: {why}" for key, why in LEGACY_KEYS.items() if key in idea
    ]
    if "stage" in idea and idea["stage"] not in STAGES:
        problems.append(f"stage '{idea['stage']}' not one of {', '.join(STAGES)}")
    state = idea.get("research_state", DEFAULT_RESEARCH_STATE)
    if state not in RESEARCH_STATES:
        problems.append(f"research_state '{state}' not one of {', '.join(RESEARCH_STATES)}")
    elif state == "shelved" and _is_blank(idea.get("revisit_when")):
        problems.append("research_state shelved requires a non-empty revisit_when")
    return problems


def evidence_problems(idea: dict[str, Any], runs: Sequence[dict[str, Any]]) -> list[str]:
    """Problems with a paper or live stage that the run evidence does not support."""
    stage = idea.get("stage")
    if stage not in EVIDENCE_STAGES:
        return []
    problems = [LIVE_BLOCKED] if stage == "live" else []
    version, run_id = idea.get("promoted_version"), idea.get("promoted_run_id")
    for key, value in (("promoted_version", version), ("promoted_run_id", run_id)):
        if value is None:
            problems.append(f"stage {stage} requires {key}")
    if version is None or run_id is None:
        return problems
    run = next((r for r in runs if r.get("run_id") == run_id), None)
    if run is None:
        problems.append(f"promoted_run_id '{run_id}' is not a run of this idea")
        return problems
    problems.extend(f"promoted_run_id '{run_id}' {b}" for b in run_blockers(run, version))
    return problems
