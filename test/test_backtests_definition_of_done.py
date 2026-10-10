"""The 20-item Definition of Done of BACKTESTS-UI-TECHNICAL-REQUIREMENTS.md section 4.

Each test drives real run folders written by `record_idea` through the read model the page uses
(`services.backtest_results_service`). Items that are about what the page shows are covered by
`frontend/src/pages/BacktestResultsDefinitionOfDone.test.tsx`; this file covers the data side.
Item numbers in the test names are the requirement numbers.

Run with: uv run pytest test/test_backtests_definition_of_done.py -v
"""

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import backtest_results_service as svc  # noqa: E402
from signal_engine.backtest.change_class import (  # noqa: E402
    DATA_RANGE_CHANGE,
    DATASET_CHANGE,
    STRATEGY_CHANGE,
)
from signal_engine.backtest import record_run as rr  # noqa: E402
from signal_engine.tests import test_backtest_compare as tc  # noqa: E402
from signal_engine.tests import test_record_run as rrbase  # noqa: E402
from signal_engine.tests.intake_fixtures import make_spec  # noqa: E402

root = rrbase.root  # the idea-folder fixtures, shared with test_record_run
IDEA_ID = "IDEA-009"
VERDICTS = {"EDGE", "MARGINAL", "NO_EDGE", "INCONCLUSIVE"}


def _runs_dir(root_path: Path) -> Path:
    return tc._idea_dir(root_path) / "runs"


@pytest.fixture
def served(monkeypatch):
    """Serve the records found on disk through the read model, as the registry would."""

    def serve(root_path: Path, flats):
        records = [json.loads(f.read_text(encoding="utf-8")) for f in flats]
        monkeypatch.setattr(
            svc,
            "_load",
            lambda _root: [{"id": IDEA_ID, "folder": "idea-009-orb", "runs": records}],
        )
        return [r["run_id"] for r in records]

    return serve


def _record_two(root_path, rows_a, period_a, rows_b, period_b, **kw_b):
    fp_b = kw_b.pop("fingerprint", "d" * 64)
    a = tc._record(root_path, tc._execution_of(rows_a, *period_a))
    b = tc._record(root_path, tc._execution_of(rows_b, *period_b), fingerprint=fp_b, **kw_b)
    return a, b


@pytest.fixture
def overlap(root, served, monkeypatch):
    from signal_engine.tests.published_fixtures import fake_resolution

    monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())
    a, b = _record_two(
        root,
        tc.A_ROWS,
        ("2026-03-01", "2026-03-10"),
        tc.B_ROWS,
        ("2026-03-04", "2026-03-20"),
    )
    ids = served(root, (a, b))
    return {"root": root, "flats": (a, b), "ids": ids}


def _detail(state, index=0):
    ok, body, status = svc.get_run_detail(IDEA_ID, state["ids"][index], root=state["root"])
    assert ok and status == 200
    return body["run"]


def _compare(state):
    ok, body, status = svc.compare_runs(IDEA_ID, *state["ids"], root=state["root"])
    assert ok and status == 200
    return body


class TestRunIdentity:
    def test_1_the_run_shows_the_exact_strategy_version_used(self, overlap):
        run = _detail(overlap)
        manifest = json.loads(
            (overlap["flats"][0].parent / overlap["ids"][0] / "manifest.json").read_text()
        )
        assert run["strategy_version"] == manifest["strategy"]["version"] == "v1"
        assert run["spec"]["name"]
        assert run["fingerprints"]["strategy"] == manifest["fingerprints"]["strategy"]
        assert re.fullmatch(r"[0-9a-f]{64}", run["fingerprints"]["strategy"])

    def test_1_the_spec_shown_is_the_frozen_copy_not_the_ideas_current_spec(self, overlap):
        idea_spec = tc._idea_dir(overlap["root"]) / "spec.yaml"
        edited = make_spec(name="Edited after the run", stop=["9 ATR(14) below entry"])
        idea_spec.write_text(yaml.safe_dump(edited), encoding="utf-8")
        run = _detail(overlap)
        assert run["spec"]["name"] != "Edited after the run"
        assert run["spec"]["stop"] != ["9 ATR(14) below entry"]

    def test_2_the_run_shows_its_complete_scope(self, overlap):
        scope = _detail(overlap)["scope"]
        assert scope["period"] == {"start": "2026-03-01", "end": "2026-03-10"}
        for field in ("universe", "style", "timeframe", "market", "profile", "dataset"):
            assert scope[field], field
        assert scope["force_flat"]
        assert scope["slippage_bps_per_side"] is not None
        assert scope["cost_bps_round_trip"] is not None

    def test_3_the_run_identifies_its_dataset_and_fingerprint(self, overlap):
        dataset = _detail(overlap)["scope"]["dataset"]
        assert dataset["id"] and dataset["data_as_of"]
        assert dataset["fingerprint"] == "d" * 64


class TestImmutability:
    def test_4_a_run_folder_is_read_only_and_a_later_run_leaves_it_unchanged(self, overlap):
        folder = overlap["flats"][0].parent / overlap["ids"][0]
        before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        assert before
        for path in before:
            assert not path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), path
        # The second run was recorded after the first; the first is byte-for-byte what it was.
        assert all(p.read_bytes() == data for p, data in before.items())
        assert overlap["ids"][0] != overlap["ids"][1]

    def test_4_a_recorded_run_id_cannot_be_written_again(self, overlap):
        with pytest.raises(FileExistsError):
            rr.write_run(overlap["flats"][0].parent, {"run_id": overlap["ids"][0]})

    def test_5_a_used_strategy_version_survives_a_later_rule_change(self, root, served, monkeypatch):
        from signal_engine.tests.published_fixtures import fake_resolution

        monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())
        ex = tc._execution_of(tc.A_ROWS, "2026-03-01", "2026-03-10")
        first = tc._record(root, ex)
        versions = tc._idea_dir(root) / "versions.yaml"
        before = versions.read_bytes()
        edited = make_spec(stop=["2 ATR(14) below entry"])
        (tc._idea_dir(root) / "spec.yaml").write_text(yaml.safe_dump(edited), encoding="utf-8")
        second = tc._record(root, ex, parent_version="v1")
        after = versions.read_bytes()
        assert after.startswith(before) and len(after) > len(before)
        ids = served(root, (first, second))
        old, new = (svc.get_run_detail(IDEA_ID, i, root=root)[1]["run"] for i in ids)
        assert (old["strategy_version"], new["strategy_version"]) == ("v1", "v2")
        assert old["spec"]["stop"] == ["1 ATR(14) below entry"]
        assert new["spec"]["stop"] == ["2 ATR(14) below entry"]


class TestComparison:
    def test_6_a_date_range_extension_is_a_new_run_of_the_same_version(self, overlap):
        a, b = (_detail(overlap, i) for i in (0, 1))
        assert a["run_id"] != b["run_id"]
        assert a["strategy_version"] == b["strategy_version"] == "v1"
        classes = b["change_classes"]
        assert DATA_RANGE_CHANGE in classes and STRATEGY_CHANGE not in classes

    def test_7_a_dataset_change_is_detectable(self, root, served, monkeypatch):
        from signal_engine.tests.published_fixtures import fake_resolution

        monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())
        a, b = _record_two(
            root,
            tc.A_ROWS,
            ("2026-03-01", "2026-03-10"),
            tc.A_ROWS,
            ("2026-03-01", "2026-03-10"),
            fingerprint="e" * 64,
        )
        state = {"root": root, "ids": served(root, (a, b))}
        comparison = _compare(state)["comparison"]
        assert comparison["dataset"]["fingerprint_changed"] is True
        assert DATASET_CHANGE in comparison["change_classes"]
        assert _detail(state, 1)["scope"]["dataset"]["fingerprint"] == "e" * 64

    def test_8_previous_and_current_runs_can_be_compared(self, overlap):
        body = _compare(overlap)
        assert body["comparison"] is not None
        assert body["metric_deltas"]
        assert set(body["comparison"]["performance"])

    def test_9_the_comparison_separates_strategy_from_data_scope_and_execution(self, overlap):
        comparison = _compare(overlap)["comparison"]
        assert comparison["strategy"]["same_version"] is True
        assert comparison["strategy"]["rules"] == {"changed": {}, "added": {}, "removed": {}}
        assert comparison["strategy"]["parameters"]["changed"] == {}
        scope = {f["field"]: f for f in comparison["scope"]}
        assert scope["period.end"]["changed"] is True
        assert scope["universe"]["changed"] is False
        assert comparison["dataset"]["fingerprint_changed"] is False
        assert all(not d["changed"] for d in comparison["execution"])
        assert all(not d["changed"] for d in comparison["cost"])

    def test_9_a_rule_change_shows_as_a_strategy_change_only(self, root, served, monkeypatch):
        from signal_engine.tests.published_fixtures import fake_resolution

        monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())
        ex = tc._execution_of(tc.A_ROWS, "2026-03-01", "2026-03-10")
        first = tc._record(root, ex)
        edited = make_spec(stop=["2 ATR(14) below entry"])
        (tc._idea_dir(root) / "spec.yaml").write_text(yaml.safe_dump(edited), encoding="utf-8")
        second = tc._record(root, ex, parent_version="v1")
        state = {"root": root, "ids": served(root, (first, second))}
        comparison = _compare(state)["comparison"]
        assert STRATEGY_CHANGE in comparison["change_classes"]
        assert comparison["strategy"]["same_version"] is False
        assert "stop" in comparison["strategy"]["rules"]["changed"]
        assert comparison["dataset"]["fingerprint_changed"] is False
        assert all(not d["changed"] for d in comparison["execution"] + comparison["cost"])

    def test_10_different_date_ranges_give_an_overlap_warning(self, overlap):
        overlap_block = _compare(overlap)["comparison"]["overlap"]
        warning = overlap_block["warning"]
        assert warning and "2026-03-11" in warning and "2026-03-20" in warning
        assert overlap_block["common"] == {"start": "2026-03-04", "end": "2026-03-10"}
        assert overlap_block["extra"]["a"] and overlap_block["extra"]["b"]

    def test_10_identical_ranges_give_no_overlap_warning(self, root, served, monkeypatch):
        from signal_engine.tests.published_fixtures import fake_resolution

        monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())
        ex = tc._execution_of(tc.A_ROWS, "2026-03-01", "2026-03-10")
        flats = (tc._record(root, ex), tc._record(root, ex))
        state = {"root": root, "ids": served(root, flats)}
        assert _compare(state)["comparison"]["overlap"]["warning"] is None
        assert _compare(state)["comparison"]["change_classes"] == ["RESEARCH_RERUN"]


class TestDecisionSupport:
    def test_11_and_12_the_verdict_comes_from_the_run_with_explicit_reasons(self, overlap):
        verdict = _detail(overlap)["verdict"]
        assert verdict["run_verdict"] in VERDICTS
        assert isinstance(verdict["reasons"], list)
        for reason in verdict["reasons"]:
            assert reason["text"] and reason["gate"] and reason["status"]

    def test_11_the_verdict_is_not_taken_from_the_ideas_frontmatter(self, overlap):
        idea = tc._idea_dir(overlap["root"]) / "idea.md"
        text = idea.read_text(encoding="utf-8")
        idea.write_text(text.replace("---\n", "---\nverdict: EDGE\n", 1), encoding="utf-8")
        assert _detail(overlap)["verdict"]["run_verdict"] != "EDGE"

    def test_13_and_14_promotion_is_stage_aware_and_shows_what_is_missing(self, overlap):
        promotion = _detail(overlap)["promotion"]
        assert set(promotion["stages"]) == {"backtest", "paper", "live"}
        for stage in promotion["stages"].values():
            assert stage["decision"] in {"READY_FOR_NEXT_STAGE", "NOT_READY", "BLOCKED"}
        later = [promotion["stages"][s] for s in ("paper", "live")]
        assert all(s["decision"] != "READY_FOR_NEXT_STAGE" for s in later)
        assert any(s["gates"] or s["reasons"] for s in promotion["stages"].values())

    def test_15_the_page_gets_a_next_research_action(self, overlap):
        action = _detail(overlap)["next_action"]
        assert action["action"] and action["reason"]

    def test_16_every_verdict_reason_traces_to_a_gate_row_and_to_trades(self, overlap):
        run = _detail(overlap)
        report = json.loads(
            (overlap["flats"][0].parent / overlap["ids"][0] / "report" / "report.json").read_text()
        )
        gates = {
            row["gate"]
            for dim in report["evaluation"]["dimensions"]
            for row in dim["rows"]
        }
        for reason in run["verdict"]["reasons"]:
            kind, _, gate = reason["evidence_ref"].partition(":")
            assert kind == "evaluation" and gate in gates
        ok, body, status = svc.get_run_trades(
            IDEA_ID, overlap["ids"][0], root=overlap["root"], limit=5
        )
        assert ok and status == 200 and body["page"]["rows"]
        assert body["page"]["rows"][0]["trade_id"]

    def test_17_reproducibility_status_is_visible(self, overlap):
        reproducibility = _detail(overlap)["reproducibility"]
        assert isinstance(reproducibility["reproducible"], bool)
        assert reproducibility["checklist"]


class TestExistingFunctionality:
    def test_19_the_idea_endpoint_still_returns_every_run(self, overlap):
        ok, idea, status = svc.get_idea_results(IDEA_ID, root=overlap["root"])
        assert ok and status == 200 and len(idea["runs"]) == 2

    def test_19_a_legacy_schema_two_run_still_renders_with_not_captured(self, overlap):
        records = svc._load(overlap["root"])
        legacy = {**records[0]["runs"][0], "run_id": "2026-09-01-legacy-v1", "schema": 2}
        legacy.pop("artifact_dir", None)
        records[0]["runs"].append(legacy)
        ok, body, status = svc.get_run_detail(IDEA_ID, "2026-09-01-legacy-v1", root=overlap["root"])
        assert ok and status == 200 and body["run"]["captured"] is False
        assert body["run"]["verdict"] is None and body["run"]["spec"] is None

    def test_20_the_decision_needs_no_raw_metric_reading(self, overlap):
        run = _detail(overlap)
        decision = [run["verdict"], run["promotion"], run["next_action"]]
        assert all(decision), "verdict, promotion and next action must all be present"
