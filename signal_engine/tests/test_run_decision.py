"""Researcher keep/reject decision: recorded beside a run, never read by verdict logic."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from signal_engine.research.intake import cli, decisions
from signal_engine.tests.test_registry_stage import RUN_ID, _entry, _idea, _setup

SIGNAL_ROOT = Path(__file__).resolve().parents[1]
#: Every module that computes a verdict, a promotion decision or a next action.
VERDICT_MODULES = (
    "backtest/verdict.py",
    "backtest/promotion.py",
    "backtest/next_action.py",
    "backtest/evaluation.py",
    "research/intake/lifecycle.py",
    "research/build_registry.py",
)


@pytest.fixture
def folder(tmp_path):
    idea = _idea(tmp_path, "stage: backtest\n", spec=True)
    _setup(idea)
    return idea


class TestRecord:
    def test_a_decision_is_appended_and_read_back(self, folder):
        entry = decisions.record(folder, RUN_ID, "keep", note="clean gross edge")
        assert entry["run_id"] == RUN_ID and entry["decision"] == "keep"
        assert decisions.load(folder) == [entry]
        assert decisions.latest(folder, RUN_ID) == entry

    def test_the_latest_decision_wins_and_history_is_kept(self, folder):
        decisions.record(folder, RUN_ID, "keep")
        decisions.record(folder, RUN_ID, "reject", note="costs eat it")
        assert [e["decision"] for e in decisions.load(folder)] == ["keep", "reject"]
        assert decisions.latest(folder, RUN_ID)["decision"] == "reject"

    def test_earlier_text_is_never_rewritten(self, folder):
        decisions.record(folder, RUN_ID, "keep")
        before = (folder / decisions.DECISIONS_FILE).read_bytes()
        decisions.record(folder, RUN_ID, "reject")
        assert (folder / decisions.DECISIONS_FILE).read_bytes().startswith(before)

    def test_only_keep_and_reject_are_accepted(self, folder):
        with pytest.raises(decisions.DecisionError, match="keep or reject"):
            decisions.record(folder, RUN_ID, "maybe")
        assert not (folder / decisions.DECISIONS_FILE).exists()

    def test_an_unknown_run_is_refused(self, folder):
        with pytest.raises(decisions.DecisionError, match="no run"):
            decisions.record(folder, "2026-01-01-nope-v1", "keep")
        assert not (folder / decisions.DECISIONS_FILE).exists()

    def test_no_decision_means_none(self, folder):
        assert decisions.load(folder) == []
        assert decisions.latest(folder, RUN_ID) is None

    def test_the_decision_file_is_not_inside_the_sealed_run(self, folder):
        decisions.record(folder, RUN_ID, "keep")
        assert not list((folder / "runs").rglob(decisions.DECISIONS_FILE))


class TestCli:
    def test_decide_records_a_decision(self, tmp_path, folder):
        code = cli.main(
            ["decide", "IDEA-001", RUN_ID, "reject", "--note", "no edge"], root=tmp_path
        )
        assert code == 0
        assert decisions.latest(folder, RUN_ID)["decision"] == "reject"

    def test_decide_refuses_a_bad_value(self, tmp_path, folder):
        with pytest.raises(SystemExit):
            cli.main(["decide", "IDEA-001", RUN_ID, "maybe"], root=tmp_path)


class TestVerdictIndependence:
    """The decision must never influence the calculated verdict."""

    @staticmethod
    def _calculated(tmp_path):
        idea = _entry(tmp_path)
        return {
            k: idea.get(k) for k in ("verdict", "status", "stage", "research_state", "next_action")
        } | {"latest_run": (idea.get("runs") or [{}])[-1].get("verdict")}

    def test_changing_the_decision_never_changes_the_calculated_result(self, tmp_path, folder):
        none_recorded = self._calculated(tmp_path)
        decisions.record(folder, RUN_ID, "keep")
        kept = self._calculated(tmp_path)
        decisions.record(folder, RUN_ID, "reject", note="changed my mind")
        rejected = self._calculated(tmp_path)
        assert none_recorded == kept == rejected

    def test_no_verdict_module_reads_decisions(self):
        for rel in VERDICT_MODULES:
            tree = ast.parse((SIGNAL_ROOT / rel).read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            imported = {
                a.name
                for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names
            } | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            assert "decisions" not in names | attrs, rel
            assert not any("decisions" in i for i in imported), rel
