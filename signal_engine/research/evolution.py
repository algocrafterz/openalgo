"""Strategy evolution: is an idea improving, stagnating, regressing or exhausted.

Reads an idea's run records in date order and classifies the trajectory of one primary
metric. The rules are mechanical on purpose: they cannot say a strategy is good, only
whether more tuning is still buying progress. The aim is to stop endless optimisation of
a weak idea while keeping real improvement visible.

Primary metric is the net edge over the random-entry baseline (`excess_net_bps`) when
every run carries one, otherwise net bps per trade.

States:
  insufficient_history  fewer than MIN_RUNS runs
  exhausted             MIN_EXHAUSTED_RUNS or more, none ever positive, best never
                        cleared zero, and recent gains are within noise
  regressing            latest is clearly below the best run
  improving             latest clearly above the first of the recent window
  stagnating            none of the above: changes are within noise
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from signal_engine.research.build_registry import cost_basis, edge_bar

MIN_RUNS = 3
MIN_EXHAUSTED_RUNS = 4
#: A change smaller than this many bps is treated as noise.
NOISE_BPS = 5.0
RECENT_WINDOW = 3


def _metric(run: dict[str, Any], name: str) -> float | None:
    if name == "excess_net_bps":
        return (run.get("baseline") or {}).get("excess_net_bps")
    return (run.get("metrics") or {}).get("net_bps")


def assess_evolution(runs: Sequence[dict[str, Any]], verdict: str | None = None) -> dict[str, Any]:
    """Classify the trajectory of a run history. Inputs are not modified.

    The state is about direction only. The action also weighs level: a rising line whose
    latest t-statistic is below the edge bar is not worth tuning on its own, and a
    `no-edge` verdict stops the idea whatever the trend (a gain can come from correcting
    a rule rather than from finding an edge).
    """
    ordered = sorted(runs, key=lambda r: (r.get("date", ""), r.get("run_id", "")))
    metric = (
        "excess_net_bps"
        if ordered and all(_metric(r, "excess_net_bps") is not None for r in ordered)
        else "net_bps"
    )
    scored = [r for r in ordered if _metric(r, metric) is not None]
    # Nets costed on different bases are not comparable: only runs on the latest run's basis
    # count toward the trajectory, so a step across a basis change is excluded.
    same_basis = [r for r in scored if cost_basis(r) == cost_basis(scored[-1])] if scored else []
    series = [(r["run_id"], _metric(r, metric)) for r in same_basis]
    base: dict[str, Any] = {"metric": metric, "runs": len(series), "series": series}
    if len(same_basis) != len(scored):
        base["cost_basis_changed"] = True

    if len(series) < MIN_RUNS:
        return {
            **base,
            "state": "insufficient_history",
            "action": "keep_iterating",
            "best_run": None,
            "reason": f"{len(series)} comparable runs; need {MIN_RUNS} to judge a trend"
            + (
                "; earlier runs use a different cost basis"
                if len(same_basis) != len(scored)
                else ""
            ),
        }

    values = [v for _, v in series]
    best_id, best = max(series, key=lambda s: s[1])
    window = values[-RECENT_WINDOW:]
    delta = window[-1] - window[0]

    if len(series) >= MIN_EXHAUSTED_RUNS and best <= 0 and delta < NOISE_BPS:
        state, action = "exhausted", "stop_and_reject"
        reason = (
            f"{len(series)} runs, best {best:.1f} bps never above zero, "
            f"recent change {delta:+.1f} bps is within noise"
        )
    elif best - values[-1] > NOISE_BPS:
        state, action = "regressing", "revert_to_best"
        reason = f"latest {values[-1]:.1f} bps is {best - values[-1]:.1f} below best ({best_id})"
    elif delta > NOISE_BPS:
        state, action = "improving", "keep_iterating"
        reason = f"recent window moved {delta:+.1f} bps"
    else:
        state = "stagnating"
        action = "change_structure_or_stop" if best > 0 else "stop_unless_new_idea"
        reason = f"recent window moved {delta:+.1f} bps, within noise"
    latest = (ordered[-1].get("metrics") or {}) if ordered else {}
    t_stat = latest.get("t_stat")
    if state == "improving" and t_stat is not None and t_stat < edge_bar(latest):
        action = "verify_before_tuning"
        reason += f", but latest t {t_stat:.2f} is below the bar {edge_bar(latest):.2f}"
    if verdict == "no-edge" and action != "stop_and_reject":
        action = "stop_and_reject"
        reason += "; verdict is no-edge, so more tuning is not justified"
    return {**base, "state": state, "action": action, "best_run": best_id, "reason": reason}


def main() -> None:
    """Print the verdict for every idea that has runs."""
    import json
    from pathlib import Path

    from signal_engine.research.intake.lifecycle import derive_verdict

    for idea in sorted((Path(__file__).parent / "ideas").glob("idea-*")):
        runs = [json.loads(p.read_text()) for p in sorted((idea / "runs").glob("*.json"))]
        if runs:
            out = assess_evolution(runs, verdict=derive_verdict(runs[-1]))
            print(f"{idea.name}: {out['state']} -> {out['action']} ({out['reason']})")


if __name__ == "__main__":
    main()
