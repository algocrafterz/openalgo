"""Registry: stage / research_state frontmatter, derived legacy fields, evidence for stages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from signal_engine.backtest.artifacts import RunFolder
from signal_engine.research import build_registry as br
from signal_engine.research.intake import lifecycle
from signal_engine.tests.intake_fixtures import make_spec, write_spec
from signal_engine.tests.test_registry_v3 import FILES, RUN_ID, _manifest, _record, report_for

IDEA = """---
id: IDEA-001
title: Sample
{lifecycle}category: intraday-momentum
horizon: intraday
source: {{type: self}}
summary: One line.
last_reviewed: 2026-09-29
{extra}---

Body.
"""


def _setup(idea: Path, run_id: str = RUN_ID, version: str = "v1", **patch) -> dict:
    """Write a schema 3 run of `version` (flat record, manifest and artifacts) into an idea."""
    runs = idea / "runs"
    rf = RunFolder(runs, run_id)
    files = {
        **FILES,
        "assumptions.json": {**FILES["assumptions.json"], "next_id": 1},
        "report/report.json": report_for(patch),
    }
    for rel, content in files.items():
        rf.add(rel, content)

    def manifest(arts):
        base = _manifest(run_id, arts)
        return {**base, "strategy": {**base["strategy"], "version": version}}

    folder = rf.commit(manifest)
    rec = {**_record(run_id, folder), "strategy_version": version, **patch}
    (runs / f"{run_id}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _idea(root: Path, lifecycle_lines: str = "stage: inbox\n", extra: str = "", spec=False) -> Path:
    folder = root / "research" / "ideas" / "idea-001-sample"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "idea.md").write_text(
        IDEA.format(lifecycle=lifecycle_lines, extra=extra), encoding="utf-8"
    )
    if spec:
        write_spec(folder, make_spec())
    return folder


def _entry(root: Path) -> dict:
    (idea,) = br.load_ideas(root)
    return idea


class TestVocabulary:
    def test_stages_and_states(self):
        assert lifecycle.STAGES == ("inbox", "backtest", "paper", "live", "retired")
        assert lifecycle.RESEARCH_STATES == (
            "active",
            "promising",
            "needs_more_evidence",
            "weak_evidence",
            "evidence_against_edge",
            "shelved",
            "rejected",
        )


class TestFrontmatterKeys:
    def test_stage_is_required(self, tmp_path):
        _idea(tmp_path, lifecycle_lines="")
        with pytest.raises(br.RegistryError, match="missing field 'stage'"):
            br.load_ideas(tmp_path)

    def test_research_state_defaults_to_active(self, tmp_path):
        _idea(tmp_path)
        assert _entry(tmp_path)["research_state"] == "active"

    def test_unknown_stage_is_reported(self, tmp_path):
        _idea(tmp_path, "stage: evaluating\n")
        with pytest.raises(br.RegistryError, match="stage 'evaluating' not one of"):
            br.load_ideas(tmp_path)

    def test_unknown_research_state_is_reported(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nresearch_state: maybe\n")
        with pytest.raises(br.RegistryError, match="research_state 'maybe' not one of"):
            br.load_ideas(tmp_path)

    def test_legacy_status_key_is_refused_with_the_reason(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nstatus: rejected\n")
        with pytest.raises(
            br.RegistryError, match="status was split into stage and research_state"
        ):
            br.load_ideas(tmp_path)

    def test_legacy_verdict_key_is_refused_with_the_reason(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nverdict: edge\n")
        with pytest.raises(br.RegistryError, match="verdict is calculated per run"):
            br.load_ideas(tmp_path)

    def test_legacy_keys_alone_do_not_also_report_a_missing_stage_cascade(self, tmp_path):
        _idea(tmp_path, "status: rejected\nverdict: none\n")
        with pytest.raises(br.RegistryError) as err:
            br.load_ideas(tmp_path)
        text = str(err.value)
        assert "status was split" in text and "verdict is calculated" in text

    def test_shelved_requires_a_revisit_when(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nresearch_state: shelved\n")
        with pytest.raises(br.RegistryError, match="shelved requires a non-empty revisit_when"):
            br.load_ideas(tmp_path)

    @pytest.mark.parametrize("value", ['""', "'   '", "null", "[]"])
    def test_shelved_with_an_empty_revisit_when_is_refused(self, tmp_path, value):
        _idea(tmp_path, "stage: inbox\nresearch_state: shelved\n", extra=f"revisit_when: {value}\n")
        with pytest.raises(br.RegistryError, match="revisit_when"):
            br.load_ideas(tmp_path)

    def test_shelved_with_a_revisit_when_loads(self, tmp_path):
        _idea(
            tmp_path,
            "stage: inbox\nresearch_state: shelved\n",
            extra="revisit_when: After 1m data refresh.\n",
        )
        assert _entry(tmp_path)["research_state"] == "shelved"

    def test_revisit_when_is_not_needed_for_other_states(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nresearch_state: promising\n")
        assert _entry(tmp_path)["research_state"] == "promising"


class TestRejectionBasisKeysOnResearchState:
    def test_required_for_a_rejected_idea_reviewed_after_the_cutoff(self, tmp_path):
        folder = _idea(tmp_path, "stage: inbox\nresearch_state: rejected\n")
        text = (folder / "idea.md").read_text().replace("2026-09-29", "2026-10-02")
        (folder / "idea.md").write_text(text)
        with pytest.raises(br.RegistryError, match="rejection_basis is required"):
            br.load_ideas(tmp_path)

    def test_only_valid_when_research_state_is_rejected(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nresearch_state: promising\n", "rejection_basis: overfit\n")
        with pytest.raises(br.RegistryError, match="only valid when research_state is rejected"):
            br.load_ideas(tmp_path)

    def test_a_rejected_idea_with_a_basis_loads(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nresearch_state: rejected\n", "rejection_basis: overfit\n")
        assert _entry(tmp_path)["rejection_basis"] == "overfit"


class TestSpecRequirementIsStageBased:
    @pytest.mark.parametrize("stage", ["backtest", "paper", "live"])
    def test_spec_is_required_from_backtest(self, tmp_path, stage):
        _idea(tmp_path, f"stage: {stage}\n")
        with pytest.raises(br.RegistryError, match="spec.yaml is required"):
            br.load_ideas(tmp_path)

    @pytest.mark.parametrize("stage", ["inbox", "retired"])
    def test_inbox_and_retired_need_no_spec(self, tmp_path, stage):
        _idea(tmp_path, f"stage: {stage}\n")
        assert len(br.load_ideas(tmp_path)) == 1

    def test_a_rejected_backtest_idea_still_needs_a_spec(self, tmp_path):
        _idea(tmp_path, "stage: backtest\nresearch_state: rejected\n")
        with pytest.raises(br.RegistryError, match="spec.yaml is required"):
            br.load_ideas(tmp_path)

    def test_active_stages_constant(self):
        from signal_engine.research.intake import spec

        assert spec.SPEC_ACTIVE_STAGES == ("backtest", "paper", "live")
        assert not hasattr(spec, "SPEC_ACTIVE_STATUSES")

    def test_registry_problems_takes_a_stage(self, tmp_path):
        from signal_engine.research.intake import spec

        assert spec.registry_problems("IDEA-003", "backtest", tmp_path)
        assert spec.registry_problems("IDEA-003", "inbox", tmp_path) == []


class TestDerivedLegacyFields:
    @pytest.mark.parametrize(
        "stage, state, has_runs, expected",
        [
            ("inbox", "active", False, "inbox"),
            ("inbox", "active", True, "inbox"),
            ("backtest", "active", False, "evaluating"),
            ("backtest", "promising", True, "backtested"),
            ("paper", "active", True, "paper"),
            ("live", "active", True, "live"),
            ("retired", "active", True, "shelved"),
            ("backtest", "rejected", True, "rejected"),
            ("paper", "rejected", False, "rejected"),
            ("inbox", "rejected", False, "rejected"),
            ("backtest", "shelved", True, "shelved"),
            ("live", "shelved", False, "shelved"),
            ("backtest", "weak_evidence", False, "evaluating"),
            ("backtest", "evidence_against_edge", True, "backtested"),
            ("backtest", "needs_more_evidence", True, "backtested"),
        ],
    )
    def test_status_mapping(self, stage, state, has_runs, expected):
        assert lifecycle.derive_status(stage, state, has_runs) == expected

    @pytest.mark.parametrize(
        "run_verdict, expected",
        [
            ("EDGE", "edge"),
            ("MARGINAL", "marginal"),
            ("NO_EDGE", "no-edge"),
            ("INCONCLUSIVE", "inconclusive"),
            (None, "none"),
            ("SOMETHING_NEW", "none"),
            ("edge", "none"),
        ],
    )
    def test_verdict_mapping(self, run_verdict, expected):
        assert lifecycle.derive_verdict({"run_verdict": run_verdict}) == expected

    def test_verdict_without_a_run_or_field(self):
        assert lifecycle.derive_verdict(None) == "none"
        assert lifecycle.derive_verdict({}) == "none"

    def test_entry_carries_derived_status_and_verdict_and_new_keys(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nresearch_state: rejected\n")
        entry = _entry(tmp_path)
        assert entry["status"] == "rejected"
        assert entry["verdict"] == "none"
        assert entry["stage"] == "inbox" and entry["research_state"] == "rejected"

    def test_status_is_evaluating_then_backtested_once_a_run_exists(self, tmp_path):
        folder = _idea(tmp_path, "stage: backtest\n", spec=True)
        assert _entry(tmp_path)["status"] == "evaluating"
        _setup(folder, run_verdict="EDGE")
        entry = _entry(tmp_path)
        assert entry["status"] == "backtested" and entry["verdict"] == "edge"

    def test_verdict_comes_from_the_latest_run_only(self, tmp_path):
        folder = _idea(tmp_path, "stage: backtest\n", spec=True)
        _setup(folder, "2026-09-29-orb-v1", run_verdict="EDGE")
        _setup(folder, "2026-09-30-orb-v1", run_verdict="NO_EDGE", date="2026-09-30")
        assert _entry(tmp_path)["verdict"] == "no-edge"

    def test_run_without_a_run_verdict_is_none(self, tmp_path):
        folder = _idea(tmp_path, "stage: backtest\n", spec=True)
        _setup(folder, run_verdict=None)
        assert _entry(tmp_path)["verdict"] == "none"

    def test_registry_json_has_all_four_keys(self, tmp_path):
        _idea(tmp_path)
        data = json.loads(br.render_json(br.load_ideas(tmp_path)))
        item = data["ideas"][0] if isinstance(data, dict) else data[0]
        assert {"status", "verdict", "stage", "research_state"} <= set(item)

    def test_summary_and_renderers_still_work(self, tmp_path):
        _idea(tmp_path, "stage: inbox\nresearch_state: rejected\n")
        ideas = br.load_ideas(tmp_path)
        assert br.summarize(ideas)["by_status"] == {"rejected": 1}
        assert "rejected" in br.render_markdown(ideas)
        assert "rejected" in br.render_html(ideas)


class TestStageEvidence:
    """paper and live need a promoted version and a run that earned it."""

    GOOD = {"promotion_decision": "READY_FOR_NEXT_STAGE"}

    def _paper(self, tmp_path, **patch):
        folder = _idea(
            tmp_path,
            "stage: paper\n",
            extra="promoted_version: v1\npromoted_run_id: 2026-09-29-orb-v1\n",
            spec=True,
        )
        (folder / "versions.yaml").write_text(
            "- version: v1\n  strategy_fingerprint: " + "a" * 64 + "\n  parent: null\n"
            "  created_ist: x\n  note: ''\n",
            encoding="utf-8",
        )
        _setup(folder, **{**self.GOOD, **patch})
        return folder

    def test_a_qualifying_run_passes(self, tmp_path):
        self._paper(tmp_path)
        assert _entry(tmp_path)["stage"] == "paper"

    def test_missing_promoted_version_and_run_are_reported(self, tmp_path):
        _idea(tmp_path, "stage: paper\n", spec=True)
        with pytest.raises(br.RegistryError) as err:
            br.load_ideas(tmp_path)
        assert "promoted_version" in str(err.value) and "promoted_run_id" in str(err.value)

    def test_run_must_exist(self, tmp_path):
        folder = self._paper(tmp_path)
        (folder / "idea.md").write_text(
            (folder / "idea.md").read_text().replace("2026-09-29-orb-v1", "no-such-run")
        )
        with pytest.raises(br.RegistryError, match="promoted_run_id 'no-such-run' is not a run"):
            br.load_ideas(tmp_path)

    @pytest.mark.parametrize(
        "patch, message",
        [
            ({"reproducible": False, "promotion_eligible": False}, "reproducible"),
            ({"code_dirty": True, "promotion_eligible": False}, "code_dirty"),
            ({"promotion_decision": "NOT_READY"}, "READY_FOR_NEXT_STAGE"),
            ({"promotion_decision": "BLOCKED"}, "READY_FOR_NEXT_STAGE"),
            ({"promotion_decision": None}, "READY_FOR_NEXT_STAGE"),
        ],
    )
    def test_run_conditions_are_enforced(self, tmp_path, patch, message):
        self._paper(tmp_path, **patch)
        with pytest.raises(br.RegistryError, match=message):
            br.load_ideas(tmp_path)

    def test_run_must_belong_to_the_promoted_version(self, tmp_path):
        folder = self._paper(tmp_path, version="v2")
        with pytest.raises(br.RegistryError, match="belongs to v2, not v1"):
            br.load_ideas(tmp_path)
        assert folder.exists()

    def test_missing_promotion_decision_field_is_not_ready(self, tmp_path):
        folder = self._paper(tmp_path)
        path = folder / "runs" / "2026-09-29-orb-v1.json"
        rec = json.loads(path.read_text())
        del rec["promotion_decision"]
        path.write_text(json.dumps(rec))
        with pytest.raises(br.RegistryError, match="READY_FOR_NEXT_STAGE"):
            br.load_ideas(tmp_path)

    def test_schema_2_runs_never_qualify(self, tmp_path):
        from signal_engine.tests.test_research_registry import RUN, V2

        folder = _idea(
            tmp_path,
            "stage: paper\n",
            extra="promoted_version: v1\npromoted_run_id: 2026-09-29-orb-v1\n",
            spec=True,
        )
        (folder / "versions.yaml").write_text(
            "- version: v1\n  strategy_fingerprint: " + "a" * 64 + "\n  parent: null\n"
            "  created_ist: x\n  note: ''\n",
            encoding="utf-8",
        )
        (folder / "runs").mkdir()
        (folder / "runs" / "a.json").write_text(
            json.dumps({**RUN, **V2, "promotion_decision": "READY_FOR_NEXT_STAGE"})
        )
        with pytest.raises(br.RegistryError, match="schema 3"):
            br.load_ideas(tmp_path)

    def test_promoted_version_must_be_registered(self, tmp_path):
        folder = self._paper(tmp_path)
        (folder / "versions.yaml").unlink()
        with pytest.raises(br.RegistryError, match="not a version"):
            br.load_ideas(tmp_path)

    def test_live_is_always_rejected_even_with_good_evidence(self, tmp_path):
        folder = self._paper(tmp_path)
        text = (folder / "idea.md").read_text().replace("stage: paper", "stage: live")
        (folder / "idea.md").write_text(text)
        with pytest.raises(br.RegistryError, match="stage live cannot be reached"):
            br.load_ideas(tmp_path)

    def test_backtest_and_retired_need_no_promotion(self, tmp_path):
        _idea(tmp_path, "stage: retired\n")
        assert _entry(tmp_path)["stage"] == "retired"


class TestEvidenceHelper:
    RUN = {
        "run_id": "r1",
        "schema": 3,
        "reproducible": True,
        "code_dirty": False,
        "strategy_version": "v1",
        "promotion_decision": "READY_FOR_NEXT_STAGE",
    }

    def test_qualifying_run_has_no_blockers(self):
        assert lifecycle.run_blockers(self.RUN, "v1") == []

    def test_blockers_are_listed_together(self):
        run = {**self.RUN, "schema": 2, "reproducible": False, "code_dirty": True}
        assert len(lifecycle.run_blockers(run, "v1")) == 3

    def test_find_picks_the_latest_qualifying_run(self):
        runs = [
            {**self.RUN, "run_id": "r1"},
            {**self.RUN, "run_id": "r2"},
            {**self.RUN, "run_id": "r3", "code_dirty": True},
        ]
        run, problems = lifecycle.find_promotable_run(runs, "v1")
        assert run["run_id"] == "r2" and problems == []

    def test_find_with_no_qualifying_run_explains_each_candidate(self):
        runs = [{**self.RUN, "run_id": "r1", "promotion_decision": "NOT_READY"}]
        run, problems = lifecycle.find_promotable_run(runs, "v1")
        assert run is None and any("r1" in p for p in problems)

    def test_find_with_no_runs_of_the_version(self):
        run, problems = lifecycle.find_promotable_run([self.RUN], "v2")
        assert run is None and problems

    def test_find_by_id_reports_unknown_runs(self):
        run, problems = lifecycle.find_promotable_run([self.RUN], "v1", "zzz")
        assert run is None and "zzz" in problems[0]
