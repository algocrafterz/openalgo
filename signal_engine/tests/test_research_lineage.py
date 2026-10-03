"""Strategy lifecycle: rejection needs a stated basis, and every idea's run history is summarised.

The lineage is computed from the run records, never hand-edited, so "is this improving,
stagnating or regressing" is answered by the same numbers the results page shows.
"""

import json
from pathlib import Path

import pytest

from signal_engine.research import build_registry as br

IDEA = """---
id: IDEA-001
title: Sample
stage: inbox
research_state: {state}
strategy_category: Momentum
horizon: intraday
source: {{type: self}}
summary: One line.
{extra}last_reviewed: {reviewed}
---

Body.
"""


def _idea(root: Path, state="rejected", reviewed="2026-09-29", basis=None) -> Path:
    folder = root / "research" / "ideas" / "idea-001-sample"
    folder.mkdir(parents=True, exist_ok=True)
    extra = f"rejection_basis: {basis}\n" if basis else ""
    (folder / "idea.md").write_text(
        IDEA.format(state=state, reviewed=reviewed, extra=extra), encoding="utf-8"
    )
    return folder


def _run(run_id, date, net, *, t=None, parent=None, trials=1, trades=100):
    metrics = {"trades": trades, "gross_bps": net + 10.0, "net_bps": net}
    if t is not None:
        metrics["t_stat"] = t
    return {
        "run_id": run_id,
        "date": date,
        "code_version": "abc",
        "data": {"source": "h", "as_of": date, "universe": "u", "bar_size": "5m"},
        "period": {"start": "2020-01-01", "end": date},
        "costs": {"model": "flat", "round_trip_bps": 16.0},
        "metrics": metrics,
        "parent_run_id": parent,
        "n_trials": trials,
    }


def _write_runs(folder: Path, runs: list[dict]) -> None:
    (folder / "runs").mkdir(exist_ok=True)
    for r in runs:
        (folder / "runs" / f"{r['run_id']}.json").write_text(json.dumps(r), encoding="utf-8")


class TestRejectionBasis:
    def test_new_rejection_without_basis_is_refused(self, tmp_path):
        _idea(tmp_path, reviewed="2026-10-01")
        with pytest.raises(br.RegistryError, match="rejection_basis"):
            br.load_ideas(tmp_path)

    def test_new_rejection_with_basis_loads(self, tmp_path):
        _idea(tmp_path, reviewed="2026-10-01", basis="cost-dominated")
        assert br.load_ideas(tmp_path)[0]["rejection_basis"] == "cost-dominated"

    def test_rejection_reviewed_before_the_cutoff_is_exempt(self, tmp_path):
        _idea(tmp_path, reviewed="2026-09-30")
        assert len(br.load_ideas(tmp_path)) == 1

    def test_unknown_basis_is_refused_even_on_old_ideas(self, tmp_path):
        _idea(tmp_path, reviewed="2026-01-01", basis="bad-luck")
        with pytest.raises(br.RegistryError, match="rejection_basis"):
            br.load_ideas(tmp_path)

    def test_basis_on_a_non_rejected_idea_is_refused(self, tmp_path):
        _idea(tmp_path, state="active", basis="overfit")
        with pytest.raises(br.RegistryError, match="only valid when research_state is rejected"):
            br.load_ideas(tmp_path)

    def test_documented_bases(self):
        for basis in (
            "no-gross-edge",
            "cost-dominated",
            "overfit",
            "not-robust",
            "concurrency-artifact",
            "not-a-strategy",
            "duplicate",
        ):
            assert basis in br.REJECTION_BASES


class TestLineage:
    def test_no_runs_gives_none(self):
        assert br.lineage([]) is None

    def test_single_run_has_insufficient_history(self):
        out = br.lineage([_run("r1", "2026-09-01", 5.0, trials=3)])
        assert out["runs"] == 1
        assert out["trend"] == "insufficient"
        assert out["delta_vs_parent"] is None
        assert out["trials_sum"] == 3
        assert out["latest_run_id"] == "r1"

    def test_improving_chain(self):
        runs = [
            _run("r1", "2026-09-01", -5.0, t=-1.0),
            _run("r2", "2026-09-02", 0.0, t=0.2, parent="r1", trials=2),
            _run("r3", "2026-09-03", 4.0, t=1.4, parent="r2", trials=3),
        ]
        out = br.lineage(runs)
        assert out["trend"] == "improving"
        assert out["delta_vs_parent"] == {
            "parent_run_id": "r2",
            "net_bps": 4.0,
            "t_stat": pytest.approx(1.2),
            "trades": 0,
        }
        assert out["trials_sum"] == 6

    def test_regressing_chain(self):
        runs = [
            _run("r1", "2026-09-01", 5.0),
            _run("r2", "2026-09-02", 1.0, parent="r1"),
        ]
        assert br.lineage(runs)["trend"] == "regressing"

    def test_small_changes_are_stagnant(self):
        runs = [
            _run("r1", "2026-09-01", 2.0),
            _run("r2", "2026-09-02", 2.4, parent="r1"),
            _run("r3", "2026-09-03", 1.9, parent="r2"),
        ]
        assert br.lineage(runs)["trend"] == "stagnant"

    def test_delta_omits_t_when_either_side_lacks_it(self):
        runs = [_run("r1", "2026-09-01", 1.0, t=1.0), _run("r2", "2026-09-02", 3.0, parent="r1")]
        assert "t_stat" not in br.lineage(runs)["delta_vs_parent"]

    def test_trend_follows_the_latest_runs_own_parent_chain(self):
        runs = [
            _run("r1", "2026-09-01", 0.0),
            _run("r2", "2026-09-02", 50.0, parent="r1"),  # abandoned branch
            _run("r3", "2026-09-03", 3.0, parent="r1"),
        ]
        out = br.lineage(runs)
        assert out["delta_vs_parent"]["parent_run_id"] == "r1"
        assert out["trend"] == "improving"

    def test_trial_sum_counts_every_run_including_side_branches(self):
        runs = [
            _run("r1", "2026-09-01", 0.0, trials=2),
            _run("r2", "2026-09-02", 1.0, parent="r1", trials=5),
            _run("r3", "2026-09-03", 2.0, parent="r1", trials=7),
        ]
        assert br.lineage(runs)["trials_sum"] == 14

    def test_root_latest_run_after_branch_has_insufficient_trend(self):
        runs = [_run("r1", "2026-09-01", 0.0), _run("r2", "2026-09-02", 5.0)]
        assert br.lineage(runs)["trend"] == "insufficient"


class TestRegistryIntegration:
    def test_load_ideas_attaches_lineage_only_when_runs_exist(self, tmp_path):
        folder = _idea(tmp_path)
        assert "lineage" not in br.load_ideas(tmp_path)[0]
        _write_runs(
            folder,
            [_run("r1", "2026-09-01", 1.0), _run("r2", "2026-09-02", 9.0, parent="r1")],
        )
        idea = br.load_ideas(tmp_path)[0]
        assert idea["lineage"]["trend"] == "improving"

    def test_registry_json_carries_the_lineage(self, tmp_path):
        folder = _idea(tmp_path)
        _write_runs(folder, [_run("r1", "2026-09-01", 1.0)])
        data = json.loads(br.render_json(br.load_ideas(tmp_path)))
        assert data["ideas"][0]["lineage"]["runs"] == 1


class TestLineageEvidence:
    """A rising trend says nothing about whether the level is any good."""

    def _chain(self, t_last, hurdle=None):
        runs = [
            _run("r1", "2026-09-01", -5.0, t=-1.0),
            _run("r2", "2026-09-02", 4.0, t=t_last, parent="r1"),
        ]
        if hurdle is not None:
            runs[-1]["metrics"]["hurdle_t"] = hurdle
        return runs

    def test_improving_but_below_the_bar(self):
        out = br.lineage(self._chain(1.4))
        assert out["trend"] == "improving"
        assert out["edge_status"] == "below_bar"
        assert out["edge_bar"] == br.EDGE_T_FLOOR

    def test_clears_the_bar(self):
        assert br.lineage(self._chain(3.2))["edge_status"] == "clears_bar"

    def test_bar_rises_with_the_trial_hurdle(self):
        out = br.lineage(self._chain(3.2, hurdle=3.6))
        assert out["edge_bar"] == 3.6
        assert out["edge_status"] == "below_bar"

    def test_unknown_without_a_t_statistic(self):
        out = br.lineage([_run("r1", "2026-09-01", 5.0)])
        assert out["edge_status"] == "unknown"
