"""
Unit tests for services/backtest_results_service.py.

Run with: uv run pytest test/test_backtest_results_service.py -v
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import backtest_results_service as svc  # noqa: E402
from signal_engine.tests.intake_fixtures import GOOD_SPEC  # noqa: E402

IDEA_MD = """---
id: IDEA-{num}
title: Sample idea {num}
stage: inbox
research_state: rejected
strategy_category: Momentum
horizon: intraday
strategy_path: strategy_library/intraday/sample
source:
  type: video
  ref: "https://example.invalid/v"
summary: One line of what the idea is.
result: Lost money before costs.
revisit_when: Never.
last_reviewed: 2026-09-29
strategy_tags: [vwap]
---

Body.
"""


def _run(run_id="2026-09-30-s-v1", date="2026-09-30", net=-1.3, **extra):
    base = {
        "run_id": run_id,
        "date": date,
        "code_version": "abc1234",
        "data": {
            "source": "historify",
            "as_of": "2026-09-29",
            "universe": "fno-208",
            "bar_size": "5m",
        },
        "period": {"start": "2021-01-01", "end": "2026-09-25"},
        "costs": {"model": "zerodha-mis", "round_trip_bps": 9.4},
        "metrics": {
            "trades": 1200,
            "gross_bps": 8.1,
            "net_bps": net,
            "t_stat": 1.9,
            "hurdle_t": 1.97,
        },
        "params": {"or_minutes": 15, "sl_atr": 1.5},
        "n_trials": 1,
        "series": {
            "equity_r": [[f"2026-01-{d:02d}", float(d)] for d in range(1, 29)],
            "drawdown_r": [[f"2026-01-{d:02d}", 0.0] for d in range(1, 29)],
            "monthly": [{"month": "2026-01", "n": 28, "total_r": 28.0, "net_r": 1.0}],
        },
        "out_of_sample": {"trades": 300, "gross_bps": 2.0, "net_bps": -7.4},
    }
    base.update(extra)
    return base


def _add_idea(root: Path, num: str, runs=()):
    (root / "strategy_library" / "intraday" / "sample").mkdir(parents=True, exist_ok=True)
    folder = root / "research" / "ideas" / f"idea-{num}-sample"
    folder.mkdir(parents=True)
    (folder / "idea.md").write_text(IDEA_MD.format(num=num), encoding="utf-8")
    if runs:
        (folder / "runs").mkdir()
        for r in runs:
            (folder / "runs" / f"{r['run_id']}.json").write_text(json.dumps(r), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _add_idea(
        tmp_path,
        "001",
        runs=[
            _run(),
            _run(
                "2026-10-05-s-v2",
                "2026-10-05",
                net=2.5,
                parent_run_id="2026-09-30-s-v1",
                change_note="tighter stop",
                params={"or_minutes": 15, "sl_atr": 1.0, "vol_filter": True},
                n_trials=2,
            ),
        ],
    )
    _add_idea(tmp_path, "002")
    return tmp_path


class TestListResults:
    def test_lists_every_idea_with_run_counts(self, root):
        ok, body, status = svc.list_results(root)
        assert ok and status == 200 and body["status"] == "success"
        by_id = {i["id"]: i for i in body["ideas"]}
        assert set(by_id) == {"IDEA-001", "IDEA-002"}
        assert by_id["IDEA-001"]["run_count"] == 2
        assert by_id["IDEA-002"]["run_count"] == 0
        assert by_id["IDEA-002"]["latest_run"] is None

    def test_latest_run_is_most_recent_by_date_and_slim(self, root):
        _ok, body, _ = svc.list_results(root)
        latest = next(i for i in body["ideas"] if i["id"] == "IDEA-001")["latest_run"]
        assert latest["run_id"] == "2026-10-05-s-v2"
        assert latest["metrics"]["net_bps"] == 2.5
        assert latest["oos_net_bps"] == -7.4
        assert "series" not in latest
        assert 2 <= len(latest["sparkline"]) <= svc.SPARKLINE_POINTS

    def test_summary_counts(self, root):
        _ok, body, _ = svc.list_results(root)
        assert body["summary"]["total_ideas"] == 2
        assert body["summary"]["total_runs"] == 2
        assert body["summary"]["ideas_with_runs"] == 1

    def test_missing_research_dir_is_reported_not_crashed(self, tmp_path):
        ok, body, status = svc.list_results(tmp_path)
        assert ok and status == 200
        assert body["available"] is False and body["ideas"] == []

    def test_invalid_registry_returns_error(self, root):
        (root / "research" / "ideas" / "idea-002-sample" / "idea.md").write_text("no frontmatter")
        ok, body, status = svc.list_results(root)
        assert not ok and status == 500 and body["status"] == "error"

    def test_cache_refreshes_when_a_new_run_is_added(self, root):
        svc.list_results(root)
        folder = root / "research" / "ideas" / "idea-002-sample"
        (folder / "runs").mkdir()
        run = _run("2026-10-06-s-v1", "2026-10-06")
        (folder / "runs" / "x.json").write_text(json.dumps(run))
        _ok, body, _ = svc.list_results(root)
        assert next(i for i in body["ideas"] if i["id"] == "IDEA-002")["run_count"] == 1


class TestGetIdeaResults:
    def test_returns_idea_with_runs_oldest_first_including_series(self, root):
        ok, body, status = svc.get_idea_results("IDEA-001", root)
        assert ok and status == 200
        assert [r["run_id"] for r in body["runs"]] == ["2026-09-30-s-v1", "2026-10-05-s-v2"]
        assert body["runs"][0]["series"]["equity_r"]
        assert body["idea"]["title"] == "Sample idea 001"

    def test_unknown_idea_is_404(self, root):
        ok, body, status = svc.get_idea_results("IDEA-999", root)
        assert not ok and status == 404

    @pytest.mark.parametrize("bad", ["../etc", "IDEA-1", "idea-001", "IDEA-001/../x", ""])
    def test_malformed_idea_id_is_400(self, root, bad):
        ok, _body, status = svc.get_idea_results(bad, root)
        assert not ok and status == 400


class TestCompareRuns:
    def test_metric_deltas_and_param_diff(self, root):
        ok, body, status = svc.compare_runs("IDEA-001", "2026-09-30-s-v1", "2026-10-05-s-v2", root)
        assert ok and status == 200
        net = body["metric_deltas"]["net_bps"]
        assert net == {"a": -1.3, "b": 2.5, "delta": pytest.approx(3.8)}
        assert body["params"]["changed"] == {"sl_atr": {"a": 1.5, "b": 1.0}}
        assert body["params"]["added"] == {"vol_filter": True}
        assert body["params"]["removed"] == {}
        assert body["n_trials"] == {"a": 1, "b": 2}

    def test_like_for_like_runs_have_no_warnings(self, root):
        _ok, body, _ = svc.compare_runs("IDEA-001", "2026-09-30-s-v1", "2026-10-05-s-v2", root)
        assert body["comparable"] is True and body["warnings"] == []

    def test_different_universe_or_costs_warns(self, tmp_path):
        a = _run()
        b = _run(
            "2026-10-05-s-v2",
            "2026-10-05",
            data={**a["data"], "universe": "fno-40"},
            costs={"model": "x", "round_trip_bps": 20},
        )
        _add_idea(tmp_path, "001", runs=[a, b])
        _ok, body, _ = svc.compare_runs("IDEA-001", a["run_id"], b["run_id"], tmp_path)
        assert body["comparable"] is False
        joined = " ".join(body["warnings"])
        assert "universe" in joined and "cost" in joined

    def test_missing_metric_on_one_side_gives_null_delta(self, tmp_path):
        a = _run()
        b = _run("2026-10-05-s-v2", "2026-10-05")
        del b["metrics"]["t_stat"]
        _add_idea(tmp_path, "001", runs=[a, b])
        _ok, body, _ = svc.compare_runs("IDEA-001", a["run_id"], b["run_id"], tmp_path)
        assert body["metric_deltas"]["t_stat"] == {"a": 1.9, "b": None, "delta": None}

    def test_unknown_run_is_404_and_same_run_is_400(self, root):
        assert svc.compare_runs("IDEA-001", "nope", "2026-10-05-s-v2", root)[2] == 404
        assert svc.compare_runs("IDEA-001", "2026-10-05-s-v2", "2026-10-05-s-v2", root)[2] == 400

    @pytest.mark.parametrize("bad", ["../x", "a b", "", "x" * 200])
    def test_malformed_run_id_is_400(self, root, bad):
        assert svc.compare_runs("IDEA-001", bad, "2026-10-05-s-v2", root)[2] == 400

    def test_overlay_series_returned_for_both_runs(self, root):
        _ok, body, _ = svc.compare_runs("IDEA-001", "2026-09-30-s-v1", "2026-10-05-s-v2", root)
        assert body["equity_r"]["a"] and body["equity_r"]["b"]


class TestCoverage:
    """How far back a result was tested, and how stale the data behind it is."""

    TODAY = date(2026, 10, 9)

    def _latest(self, root):
        _ok, body, _ = svc.list_results(root, today=self.TODAY)
        return next(i for i in body["ideas"] if i["id"] == "IDEA-001")["latest_run"]

    def test_card_shows_the_tested_window_span_and_data_age(self, root):
        latest = self._latest(root)
        assert latest["period"] == {"start": "2021-01-01", "end": "2026-09-25"}
        assert latest["data"]["as_of"] == "2026-09-29"
        assert latest["data"]["universe"] == "fno-208" and latest["data"]["bar_size"] == "5m"
        assert latest["years"] == 5.7
        assert latest["data_age_days"] == 10

    def test_detail_runs_carry_the_same_coverage_fields(self, root):
        _ok, body, _ = svc.get_idea_results("IDEA-001", root, today=self.TODAY)
        assert all(r["years"] == 5.7 and r["data_age_days"] == 10 for r in body["runs"])
        assert body["runs"][0]["series"]["equity_r"]  # nothing else was dropped

    def test_a_snapshot_dated_after_today_is_not_negative(self, tmp_path):
        run = _run()
        run["data"] = {**run["data"], "as_of": "2030-01-01"}
        _add_idea(tmp_path, "001", runs=[run])
        _ok, body, _ = svc.list_results(tmp_path, today=self.TODAY)
        assert body["ideas"][0]["latest_run"]["data_age_days"] == 0

    def test_unparseable_dates_give_none_not_a_crash(self, tmp_path):
        run = _run()
        run["period"] = {"start": "garbage", "end": "2026-09-25"}
        run["data"] = {**run["data"], "as_of": "n/a"}
        _add_idea(tmp_path, "001", runs=[run])
        ok, body, _ = svc.list_results(tmp_path, today=self.TODAY)
        latest = body["ideas"][0]["latest_run"]
        assert ok and latest["years"] is None and latest["data_age_days"] is None


class TestCapAndBaselineOnTheCard:
    def test_card_carries_net_at_ten_slots_and_excess_over_random(self, tmp_path):
        run = _run(
            position_caps=[
                {"max_positions": 5, "trades": 10, "gross_bps": 1.0, "net_bps": -9.0},
                {"max_positions": 10, "trades": 20, "gross_bps": 2.0, "net_bps": -5.5},
            ],
            baseline={"gross_bps": 30.0, "net_bps": 6.0, "excess_net_bps": 12.5},
        )
        _add_idea(tmp_path, "001", runs=[run])
        _ok, body, _ = svc.list_results(tmp_path)
        latest = body["ideas"][0]["latest_run"]
        assert latest["net_bps_cap10"] == -5.5
        assert latest["baseline_excess_bps"] == 12.5

    def test_missing_cap_or_baseline_is_none(self, root):
        _ok, body, _ = svc.list_results(root)
        latest = next(i for i in body["ideas"] if i["id"] == "IDEA-001")["latest_run"]
        assert latest["net_bps_cap10"] is None and latest["baseline_excess_bps"] is None


SPEC_YAML = yaml.safe_dump(GOOD_SPEC)


class TestIdeaDetailExtras:
    def test_spec_and_research_notes_are_separate(self, root):
        folder = root / "research" / "ideas" / "idea-001-sample"
        (folder / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok
        assert body["spec"]["entry"] == GOOD_SPEC["entry"]
        assert body["research_notes"] == "Body."
        assert "id: IDEA-001" not in body["research_notes"]

    def test_idea_without_spec_reports_none(self, root):
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok and body["spec"] is None

    def test_malformed_spec_is_a_registry_error(self, root):
        folder = root / "research" / "ideas" / "idea-001-sample"
        (folder / "spec.yaml").write_text("entry: [unclosed", encoding="utf-8")
        ok, body, status = svc.get_idea_results("IDEA-001", root)
        assert not ok and status == 500 and body["status"] == "error"

    def test_cache_refreshes_when_a_spec_is_added(self, root):
        _ok, before, _ = svc.get_idea_results("IDEA-001", root)
        assert before["spec"] is None
        folder = root / "research" / "ideas" / "idea-001-sample"
        (folder / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")
        _ok, after, _ = svc.get_idea_results("IDEA-001", root)
        assert after["spec"]["name"] == GOOD_SPEC["name"]

    def test_evaluation_regimes_and_lineage_pass_through(self, tmp_path):
        evaluation = {"version": 1, "dimensions": [], "summary": {"counts": {}, "hard_failed": []}}
        regimes = {"version": 1, "trend": [], "vol": []}
        _add_idea(tmp_path, "001", runs=[_run(evaluation=evaluation, regimes=regimes)])
        ok, body, _ = svc.get_idea_results("IDEA-001", tmp_path)
        assert ok
        assert body["runs"][0]["evaluation"] == evaluation
        assert body["runs"][0]["regimes"] == regimes
        assert body["lineage"]["runs"] == 1

    def test_old_run_without_new_blocks_still_loads(self, root):
        _ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert "evaluation" not in body["runs"][0]
        assert body["runs"][0]["trades_export"] is False

    def test_rejection_basis_is_on_the_idea(self, tmp_path):
        _add_idea(tmp_path, "001", runs=[_run()])
        idea_md = tmp_path / "research" / "ideas" / "idea-001-sample" / "idea.md"
        idea_md.write_text(
            idea_md.read_text(encoding="utf-8").replace(
                "last_reviewed: 2026-09-29",
                "last_reviewed: 2026-09-29\nrejection_basis: no-gross-edge",
            ),
            encoding="utf-8",
        )
        ok, body, _ = svc.get_idea_results("IDEA-001", tmp_path)
        assert ok and body["idea"]["rejection_basis"] == "no-gross-edge"


class TestExportRun:
    RUN = "2026-09-30-s-v1"

    def test_json_is_served_exactly_as_stored(self, root):
        ok, result, status = svc.export_run("IDEA-001", self.RUN, "json", root)
        assert ok and status == 200
        stored = root / "research" / "ideas" / "idea-001-sample" / "runs" / f"{self.RUN}.json"
        assert result.path == stored
        assert result.mimetype == "application/json"
        assert result.filename == f"{self.RUN}.json"

    def test_metrics_csv_lists_section_metric_value(self, root):
        ok, result, _ = svc.export_run("IDEA-001", self.RUN, "metrics", root)
        assert ok
        lines = result.body.decode().splitlines()
        assert lines[0] == "section,metric,value"
        assert "metrics,net_bps,-1.3" in lines
        assert "out_of_sample,net_bps,-7.4" in lines

    def test_series_csv_joins_equity_and_drawdown(self, root):
        ok, result, _ = svc.export_run("IDEA-001", self.RUN, "series", root)
        assert ok
        lines = result.body.decode().splitlines()
        assert lines[0] == "date,equity_r,drawdown_r"
        assert lines[1] == "2026-01-01,1.0,0.0"
        assert len(lines) == 29

    def test_trades_404_without_a_sidecar(self, root):
        ok, body, status = svc.export_run("IDEA-001", self.RUN, "trades", root)
        assert not ok and status == 404 and body["status"] == "error"

    def test_trades_served_when_a_sidecar_exists(self, root):
        runs = root / "research" / "ideas" / "idea-001-sample" / "runs"
        (runs / f"{self.RUN}.trades.csv.gz").write_bytes(b"\x1f\x8b")
        ok, result, _ = svc.export_run("IDEA-001", self.RUN, "trades", root)
        assert ok and result.path.name == f"{self.RUN}.trades.csv.gz"
        _ok, detail, _ = svc.get_idea_results("IDEA-001", root)
        assert detail["runs"][0]["trades_export"] is True

    @pytest.mark.parametrize("run_id", ["../x", "a/b", "", "a b", "x" * 81])
    def test_invalid_run_id_is_400(self, root, run_id):
        ok, _body, status = svc.export_run("IDEA-001", run_id, "json", root)
        assert not ok and status == 400

    def test_invalid_idea_id_is_400(self, root):
        ok, _body, status = svc.export_run("../etc", self.RUN, "json", root)
        assert not ok and status == 400

    def test_unknown_format_is_400(self, root):
        ok, _body, status = svc.export_run("IDEA-001", self.RUN, "xlsx", root)
        assert not ok and status == 400

    def test_unknown_run_is_404(self, root):
        ok, _body, status = svc.export_run("IDEA-001", "no-such-run", "json", root)
        assert not ok and status == 404

    def test_formula_like_text_cells_are_defused(self):
        csv_bytes = svc._to_csv([{"a": "=1+1", "b": "ok"}], ("a", "b"))
        assert csv_bytes.decode().splitlines()[1] == "'=1+1,ok"

    def test_row_builders_match_the_backtest_export_module(self):
        export = pytest.importorskip("signal_engine.backtest.export")
        record = _run(
            in_sample={"trades": 5, "net_bps": 1.0},
            position_caps=[{"max_positions": 3, "trades": 9, "net_bps": -2.0}],
        )
        assert svc.metrics_rows(record) == export.metrics_rows(record)
        assert svc.series_rows(record) == export.series_rows(record)


# ---------------------------------------------------------------------------
# Per-run detail (schema 3 run folders) and idea-level classification fields
# ---------------------------------------------------------------------------


HEX = "a" * 64
RID = "2026-09-29-orb-v1"


def _manifest_doc(run_id=RID, **over):
    doc = {
        "schema": 3,
        "run_id": run_id,
        "created_ist": "2026-09-29T10:00:00+05:30",
        "strategy": {"version": "v2", "parent_version": "v1", "branch_label": "tight"},
        "fingerprints": dict.fromkeys(
            ("rules", "params", "strategy", "execution", "cost", "universe")
            + ("profile", "dataset"),
            HEX,
        ),
        "period": {"start": "2021-01-01", "end": "2026-09-25"},
        "dataset": {
            "id": "ds1",
            "source": "historify",
            "interval": "5m",
            "data_as_of": "2026-09-28",
            "symbol_count": 208,
            "fingerprint": HEX,
        },
        "universe": {"definition": "fno-208", "symbols_sha256": HEX, "version": "1"},
        "deployment_profile": {"name": "zerodha-mis", "product": "MIS", "sha256": HEX},
        "session": {
            "exchange": "09:15-15:30",
            "strategy_window": "09:20-14:30",
            "broker_cutoff": "15:15",
            "force_flat": "15:10",
        },
        "engine": {
            "engine_version": "1.2",
            "code_commit": "abc123",
            "code_dirty": True,
            "dirty_paths": ["a.py"],
        },
        "change_classes": ["PARAM_CHANGE"],
        "missing_capabilities": ["data_quality"],
        "reproducible": False,
        "reproducibility": {"strategy_version": True, "code_commit": False},
    }
    doc.update(over)
    return doc


SPEC = {
    "name": "orb",
    "sizing": {"capital_per_trade": 50000, "risk_per_trade_pct": 1.0},
    "portfolio": {"mode": "capped", "scope": "per-day"},
}
REPORT = {
    "verdict": {
        "run_verdict": "NO_EDGE",
        "primary_failed_gate": "G2",
        "reasons": [],
        "caps_applied": [],
    },
    "promotion": {"stages": {}, "next_stage": None},
    "next_action": {"action": "revise", "reason": "r", "gate": None},
}
STRATEGY = {"style": "breakout", "direction": "long", "timeframe": "5m", "product": "MIS"}
EXEC = {
    "resolved": {
        "execution.slippage_bps_per_side": {"value": 2.5, "ledger_id": "a1"},
        "execution.cost_bps_round_trip": {"value": 9.4, "ledger_id": "a2"},
        "profile.mis_squareoff_min": {"value": 15, "ledger_id": "a3"},
    },
    "profile": {"market": "NSE"},
}
ASSUMPTIONS = {"sealed_ist": "2026-09-29T10:00:00+05:30", "entries": [{"id": "a1"}]}


def _write(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")


def _make_run_folder(runs: Path, run_id=RID, manifest=None, skip=()):
    folder = runs / run_id
    files = {
        "manifest.json": manifest or _manifest_doc(run_id),
        "assumptions.json": ASSUMPTIONS,
        "strategy/spec.yaml": yaml.safe_dump(SPEC),
        "strategy/strategy.json": STRATEGY,
        "execution/config.json": EXEC,
        "report/report.json": REPORT,
    }
    for rel, content in files.items():
        if rel not in skip:
            _write(folder / rel, content)
    return folder


def _flat(run_id=RID, **extra):
    rec = _run(run_id)
    rec.update(schema=3, artifact_dir=f"{run_id}/", manifest_sha256=HEX)
    rec.update(extra)
    return rec


@pytest.fixture
def detail_root(tmp_path, monkeypatch):
    """A root whose registry load is stubbed: detail reads files, not the validated registry."""
    folder = tmp_path / "research" / "ideas" / "idea-001-sample"
    (folder / "runs").mkdir(parents=True)
    state = {"runs": []}

    def fake_load(_root):
        return [{"id": "IDEA-001", "folder": "idea-001-sample", "runs": state["runs"]}]

    monkeypatch.setattr(svc, "_load", fake_load)
    state["tmp"] = tmp_path
    state["runs_dir"] = folder / "runs"
    return state


def _detail(state, run_id=RID):
    return svc.get_run_detail("IDEA-001", run_id, root=state["tmp"])


class TestRunDetailFull:
    def test_every_field_of_a_schema3_run(self, detail_root):
        _make_run_folder(detail_root["runs_dir"])
        reproductions = detail_root["runs_dir"] / f"{RID}.reproductions"
        _write(
            reproductions / "20260930T100000.json",
            {"attempted_ist": "t1", "reproducible": False, "reasons": ["old"], "checks": {}},
        )
        _write(
            reproductions / "20261001T100000.json",
            {
                "run_id": RID,
                "reproducible": True,
                "reasons": [],
                "attempted_ist": "t2",
                "checks": {"x": 1},
                "compared_trades": 5,
            },
        )
        detail_root["runs"] = [_flat()]
        ok, body, status = _detail(detail_root)
        assert ok and status == 200 and body["status"] == "success"
        run = body["run"]
        assert run["run_id"] == RID and run["schema"] == 3 and run["captured"] is True
        assert run["strategy_version"] == "v2"
        assert run["parent_version"] == "v1"
        assert run["branch_label"] == "tight"
        assert run["change_classes"] == ["PARAM_CHANGE"]
        assert run["missing_capabilities"] == ["data_quality"]
        assert run["created_ist"] == "2026-09-29T10:00:00+05:30"
        assert run["fingerprints"]["rules"] == HEX and len(run["fingerprints"]) == 8
        assert run["manifest_sha256"] == HEX
        assert run["verdict"]["run_verdict"] == "NO_EDGE"
        assert run["promotion"] == REPORT["promotion"]
        assert run["next_action"]["action"] == "revise"
        assert run["spec"] == SPEC
        assert run["assumptions"] == ASSUMPTIONS
        assert run["scope"] == {
            "market": "NSE",
            "universe": "fno-208",
            "symbol_count": 208,
            "style": "breakout",
            "direction": "long",
            "timeframe": "5m",
            "product": "MIS",
            "period": {"start": "2021-01-01", "end": "2026-09-25"},
            "strategy_window": "09:20-14:30",
            "exchange_session": "09:15-15:30",
            "broker_cutoff": "15:15",
            "force_flat": "15:10",
            "capital_per_trade": 50000,
            "risk_per_trade_pct": 1.0,
            "portfolio_mode": "capped",
            "portfolio_scope": "per-day",
            "slippage_bps_per_side": 2.5,
            "cost_bps_round_trip": 9.4,
            "profile": {"name": "zerodha-mis", "sha256": HEX},
            "dataset": {
                "id": "ds1",
                "source": "historify",
                "interval": "5m",
                "data_as_of": "2026-09-28",
                "fingerprint": HEX,
            },
        }
        assert run["reproducibility"] == {
            "reproducible": False,
            "code_dirty": True,
            "dirty_paths": ["a.py"],
            "code_commit": "abc123",
            "engine_version": "1.2",
            "checklist": {"strategy_version": True, "code_commit": False},
            "last_attempt": {"attempted_ist": "t2", "reproducible": True, "reasons": []},
        }

    def test_cnc_run_and_no_strategy_window(self, detail_root):
        manifest = _manifest_doc()
        manifest["session"] = {
            "exchange": None,
            "strategy_window": "none",
            "broker_cutoff": None,
            "force_flat": None,
        }
        _make_run_folder(detail_root["runs_dir"], manifest=manifest)
        detail_root["runs"] = [_flat()]
        scope = _detail(detail_root)[1]["run"]["scope"]
        assert scope["strategy_window"] == "none"
        assert scope["broker_cutoff"] is None and scope["force_flat"] is None
        assert scope["exchange_session"] is None

    def test_portfolio_scope_may_be_absent(self, detail_root, monkeypatch):
        folder = _make_run_folder(detail_root["runs_dir"])
        spec = {**SPEC, "portfolio": {"mode": "unlimited"}}
        _write(folder / "strategy" / "spec.yaml", yaml.safe_dump(spec))
        detail_root["runs"] = [_flat()]
        scope = _detail(detail_root)[1]["run"]["scope"]
        assert scope["portfolio_mode"] == "unlimited" and scope["portfolio_scope"] is None

    def test_no_reproductions_gives_null_last_attempt(self, detail_root):
        _make_run_folder(detail_root["runs_dir"])
        detail_root["runs"] = [_flat()]
        assert _detail(detail_root)[1]["run"]["reproducibility"]["last_attempt"] is None

    def test_corrupt_newest_reproduction_gives_null(self, detail_root):
        _make_run_folder(detail_root["runs_dir"])
        repro = detail_root["runs_dir"] / f"{RID}.reproductions"
        _write(repro / "20260930T100000.json", {"attempted_ist": "t1", "reproducible": True})
        _write(repro / "20261001T100000.json", "{not json")
        detail_root["runs"] = [_flat()]
        run = _detail(detail_root)[1]["run"]
        assert run["reproducibility"]["last_attempt"] is None
        assert run["verdict"] is not None


class TestRunDetailDegraded:
    def test_legacy_schema2_is_not_captured(self, detail_root):
        detail_root["runs"] = [_run(RID)]
        ok, body, status = _detail(detail_root)
        assert ok and status == 200
        run = body["run"]
        assert run["captured"] is False and run["schema"] == 2 and run["run_id"] == RID
        for key in (
            "strategy_version",
            "parent_version",
            "branch_label",
            "verdict",
            "promotion",
            "next_action",
            "spec",
            "fingerprints",
            "scope",
            "reproducibility",
            "assumptions",
            "manifest_sha256",
            "created_ist",
        ):
            assert run[key] is None
        assert run["change_classes"] == [] and run["missing_capabilities"] == []

    def test_schema3_without_artifact_dir_is_not_captured(self, detail_root):
        rec = _flat()
        del rec["artifact_dir"]
        detail_root["runs"] = [rec]
        run = _detail(detail_root)[1]["run"]
        assert run["captured"] is False and run["schema"] == 3

    def test_missing_report_nulls_only_report_blocks(self, detail_root):
        _make_run_folder(detail_root["runs_dir"], skip=("report/report.json",))
        detail_root["runs"] = [_flat()]
        run = _detail(detail_root)[1]["run"]
        assert run["verdict"] is None and run["promotion"] is None and run["next_action"] is None
        assert run["scope"] is not None and run["spec"] == SPEC
        assert run["strategy_version"] == "v2" and run["captured"] is True

    def test_unreadable_manifest_nulls_manifest_blocks(self, detail_root):
        folder = _make_run_folder(detail_root["runs_dir"])
        (folder / "manifest.json").write_text("{broken", encoding="utf-8")
        detail_root["runs"] = [_flat()]
        run = _detail(detail_root)[1]["run"]
        assert run["strategy_version"] is None and run["fingerprints"] is None
        assert run["reproducibility"] is None and run["created_ist"] is None
        assert run["change_classes"] == [] and run["missing_capabilities"] == []
        assert run["manifest_sha256"] == HEX
        assert run["verdict"]["run_verdict"] == "NO_EDGE"
        assert run["spec"] == SPEC and run["assumptions"] == ASSUMPTIONS
        assert run["captured"] is True

    def test_missing_spec_and_assumptions_null_their_blocks(self, detail_root):
        _make_run_folder(detail_root["runs_dir"], skip=("strategy/spec.yaml", "assumptions.json"))
        detail_root["runs"] = [_flat()]
        run = _detail(detail_root)[1]["run"]
        assert run["spec"] is None and run["assumptions"] is None
        assert run["verdict"] is not None


class TestRunDetailErrors:
    def test_unknown_idea_is_404(self, detail_root):
        ok, body, status = svc.get_run_detail("IDEA-099", RID, root=detail_root["tmp"])
        assert not ok and status == 404 and body["status"] == "error"

    def test_unknown_run_is_404(self, detail_root):
        detail_root["runs"] = [_flat()]
        ok, _body, status = _detail(detail_root, "2026-09-29-orb-v9")
        assert not ok and status == 404

    @pytest.mark.parametrize(
        "bad",
        ["../x", "a/b", "2026-09-29-ORB-v1", "2026-09-29-orb-v1\n", "", "x", "2026-09-29-orb-v"],
    )
    def test_invalid_run_id_is_400_before_any_lookup(self, detail_root, bad, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("must not touch the registry")

        monkeypatch.setattr(svc, "_find_idea", boom)
        ok, body, status = svc.get_run_detail("IDEA-001", bad, root=detail_root["tmp"])
        assert not ok and status == 400 and body["message"] == "invalid run id"

    def test_absolute_artifact_dir_is_400(self, detail_root, tmp_path):
        outside = tmp_path / "outside"
        _make_run_folder(outside, RID)
        detail_root["runs"] = [_flat(artifact_dir=f"{outside / RID}/")]
        ok, _b, status = _detail(detail_root)
        assert not ok and status == 400

    def test_dotdot_artifact_dir_is_400(self, detail_root, tmp_path):
        _make_run_folder(tmp_path / "research" / "ideas", RID)
        detail_root["runs"] = [_flat(artifact_dir=f"../{RID}/")]
        ok, _b, status = _detail(detail_root)
        assert not ok and status == 400

    def test_symlink_out_of_runs_dir_is_400(self, detail_root, tmp_path):
        outside = tmp_path / "elsewhere"
        _make_run_folder(outside, RID)
        (detail_root["runs_dir"] / RID).symlink_to(outside / RID, target_is_directory=True)
        detail_root["runs"] = [_flat()]
        ok, _b, status = _detail(detail_root)
        assert not ok and status == 400


class TestIdeaCardClassification:
    def test_card_carries_new_fields_unchanged(self):
        idea = {
            "id": "IDEA-001",
            "stage": "backtest",
            "research_state": "active",
            "strategy_category": "breakout",
            "strategy_tags": ["orb"],
            "classification": {"strategy_category": "breakout", "strategy_tags": ["orb"]},
            "runs": [],
        }
        card = svc._idea_card(idea, date(2026, 10, 1))
        assert card["stage"] == "backtest" and card["research_state"] == "active"
        assert card["strategy_category"] == "breakout" and card["strategy_tags"] == ["orb"]
        assert card["classification"]["strategy_category"] == "breakout"
        assert card["id"] == "IDEA-001" and card["run_count"] == 0

    def test_old_idea_gets_none(self, root):
        _ok, body, _ = svc.list_results(root)
        card = body["ideas"][0]
        for key in (
            "stage",
            "research_state",
            "strategy_category",
            "strategy_tags",
            "classification",
        ):
            assert key in card
        assert card["classification"] is None or isinstance(card["classification"], dict)

    def test_idea_results_card_has_fields(self, root):
        _ok, body, _ = svc.get_idea_results("IDEA-001", root)
        for key in (
            "stage",
            "research_state",
            "strategy_category",
            "strategy_tags",
            "classification",
        ):
            assert key in body["idea"]


class TestRunDetailHardening:
    def _setup(self, state):
        folder = _make_run_folder(state["runs_dir"])
        state["runs"] = [_flat()]
        return folder

    def test_symlinked_runs_dir_is_rejected(self, detail_root, tmp_path):
        outside = tmp_path / "outside_runs"
        _make_run_folder(outside)
        detail_root["runs_dir"].rmdir()
        detail_root["runs_dir"].symlink_to(outside, target_is_directory=True)
        detail_root["runs"] = [_flat()]
        ok, body, status = _detail(detail_root)
        assert status == 400 and not ok

    def test_symlinked_manifest_is_unreadable(self, detail_root, tmp_path):
        folder = self._setup(detail_root)
        secret = tmp_path / "secret.json"
        secret.write_text(json.dumps(_manifest_doc()), encoding="utf-8")
        (folder / "manifest.json").unlink()
        (folder / "manifest.json").symlink_to(secret)
        _ok, body, status = _detail(detail_root)
        run = body["run"]
        assert status == 200
        assert run["scope"] is None and run["reproducibility"] is None
        assert run["strategy_version"] is None and run["fingerprints"] is None

    def test_symlinked_report_is_unreadable(self, detail_root, tmp_path):
        folder = self._setup(detail_root)
        secret = tmp_path / "r.json"
        secret.write_text(json.dumps(REPORT), encoding="utf-8")
        (folder / "report" / "report.json").unlink()
        (folder / "report" / "report.json").symlink_to(secret)
        _ok, body, _ = _detail(detail_root)
        assert body["run"]["verdict"] is None

    def test_artifact_dir_of_another_run_is_rejected(self, detail_root):
        self._setup(detail_root)
        _make_run_folder(detail_root["runs_dir"], run_id="2026-09-30-other-v1")
        detail_root["runs"] = [_flat(artifact_dir="2026-09-30-other-v1/")]
        _ok, _body, status = _detail(detail_root)
        assert status == 400

    def test_artifact_dir_subdirectory_is_rejected(self, detail_root):
        folder = self._setup(detail_root)
        (folder / "sub").mkdir()
        detail_root["runs"] = [_flat(artifact_dir=f"{RID}/sub")]
        _ok, _body, status = _detail(detail_root)
        assert status == 400

    def test_oversized_file_nulls_only_its_blocks(self, detail_root):
        folder = self._setup(detail_root)
        (folder / "manifest.json").write_text(
            json.dumps({"pad": "x" * (3 * 1024 * 1024)}), encoding="utf-8"
        )
        _ok, body, status = _detail(detail_root)
        assert status == 200
        assert body["run"]["scope"] is None
        assert body["run"]["verdict"] == REPORT["verdict"]

    def test_deeply_nested_json_does_not_raise(self, detail_root):
        folder = self._setup(detail_root)
        (folder / "report" / "report.json").write_text("[" * 100000, encoding="utf-8")
        _ok, body, status = _detail(detail_root)
        assert status == 200 and body["run"]["verdict"] is None
        assert body["run"]["scope"] is not None

    def test_spec_with_int_key_and_date_is_jsonable(self, detail_root):
        folder = self._setup(detail_root)
        (folder / "strategy" / "spec.yaml").write_text(
            "name: orb\nsince: 2026-01-02\nlevels:\n  1: a\n  2026-01-03: b\n",
            encoding="utf-8",
        )
        _ok, body, _ = _detail(detail_root)
        spec = body["run"]["spec"]
        json.dumps(spec)
        assert spec["since"] == "2026-01-02"
        assert spec["levels"]["1"] == "a" and spec["levels"]["2026-01-03"] == "b"

    def test_many_reproduction_files_are_bounded(self, detail_root):
        self._setup(detail_root)
        repro = detail_root["runs_dir"] / f"{RID}.reproductions"
        repro.mkdir()
        for i in range(5000):
            (repro / f"junk{i:05d}.txt").write_text("x")
        (repro / "20260930T100000.json").write_text(
            json.dumps({"attempted_ist": "t", "reproducible": True, "reasons": []})
        )
        seen = []
        real = svc.itertools.islice

        def spy(it, n):
            seen.append(n)
            return real(it, n)

        svc.itertools.islice = spy
        try:
            _ok, body, status = _detail(detail_root)
        finally:
            svc.itertools.islice = real
        assert status == 200 and seen and max(seen) <= 1000

    def test_malformed_manifest_shapes_become_null(self, detail_root):
        manifest = _manifest_doc(
            reproducibility={"a": True, "b": "yes", "c": 1},
            period={"start": 1, "end": "2026-01-01"},
            deployment_profile={"name": 5, "sha256": HEX},
            session={"strategy_window": 7, "exchange": 3},
            dataset={"id": 9, "source": "h", "interval": ["5m"], "symbol_count": 3},
            engine={"dirty_paths": "a.py", "code_commit": "c", "engine_version": "1"},
        )
        _make_run_folder(detail_root["runs_dir"], manifest=manifest)
        detail_root["runs"] = [_flat()]
        _ok, body, _ = _detail(detail_root)
        run = body["run"]
        assert run["reproducibility"]["checklist"] == {"a": True}
        assert run["reproducibility"]["dirty_paths"] == []
        assert run["scope"]["period"] is None
        assert run["scope"]["profile"] is None
        assert run["scope"]["strategy_window"] is None
        assert run["scope"]["dataset"]["id"] is None
        assert run["scope"]["dataset"]["interval"] is None
        assert run["scope"]["dataset"]["source"] == "h"

    def test_valid_shapes_are_kept(self, detail_root):
        self._setup(detail_root)
        _ok, body, _ = _detail(detail_root)
        scope = body["run"]["scope"]
        assert scope["period"] == {"start": "2021-01-01", "end": "2026-09-25"}
        assert scope["profile"] == {"name": "zerodha-mis", "sha256": HEX}
        assert body["run"]["reproducibility"]["dirty_paths"] == ["a.py"]


# ---- comparison engine wiring (build order 6, P2) ---------------------------------------------

import re  # noqa: E402
import subprocess  # noqa: E402

from signal_engine.tests import test_backtest_compare as tc  # noqa: E402
from signal_engine.tests import test_record_run as rrbase  # noqa: E402

TS_FILE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "backtestResults.ts"
_stub_provenance = rrbase._stub_provenance  # autouse: roots here are not git checkouts

LEGACY_KEYS = (
    "status idea_id run_a run_b metric_deltas params n_trials comparable warnings info equity_r"
).split()


def _ts_keys(interface: str) -> list[str]:
    text = TS_FILE.read_text(encoding="utf-8")
    body = re.search(rf"export interface {interface} \{{(.*?)\n\}}", text, re.S).group(1)
    return re.findall(r"^  (\w+)\??:", body, re.M)


@pytest.fixture
def real_pair(tmp_path, monkeypatch):
    """Two real run folders written by record_idea, served through a stubbed registry load."""
    from signal_engine.backtest import record_run as rr
    from signal_engine.tests.published_fixtures import fake_resolution

    # record_idea needs a published dataset version (plan Phase 4); these runs use a fake
    # execution, so a READY resolution that reads no store stands in for it.
    monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())
    rrbase._make_idea(tmp_path, "009", "orb", "backtest/strategies/orb.py")
    ex = tc._execution_of(tc.A_ROWS, "2026-03-01", "2026-03-10")
    ex_b = tc._execution_of(tc.B_ROWS, "2026-03-04", "2026-03-20")
    flat_a = tc._record(tmp_path, ex)
    flat_b = tc._record(tmp_path, ex_b)
    records = [json.loads(f.read_text(encoding="utf-8")) for f in (flat_a, flat_b)]
    state = {
        "root": tmp_path,
        "records": records,
        "ids": [r["run_id"] for r in records],
        "runs_dir": flat_a.parent,
    }
    monkeypatch.setattr(
        svc,
        "_load",
        lambda _root: [{"id": "IDEA-009", "folder": "idea-009-orb", "runs": state["records"]}],
    )
    return state


def _compare(state, a=0, b=1):
    return svc.compare_runs("IDEA-009", state["ids"][a], state["ids"][b], root=state["root"])


class TestResolvedPrefix:
    def test_fixture_shape_is_the_real_prefixed_one(self, real_pair):
        folder = real_pair["runs_dir"] / real_pair["ids"][0]
        resolved = json.loads((folder / "execution" / "config.json").read_text())["resolved"]
        assert "execution.slippage_bps_per_side" in resolved

    def test_scope_slippage_and_cost_are_not_null(self, real_pair):
        _ok, body, _ = svc.get_run_detail("IDEA-009", real_pair["ids"][0], root=real_pair["root"])
        scope = body["run"]["scope"]
        assert scope["slippage_bps_per_side"] is not None
        assert scope["cost_bps_round_trip"] is not None

    def test_service_scope_equals_compare_build_scope_for_every_field(self, real_pair):
        from signal_engine.backtest import compare

        for rid in real_pair["ids"]:
            _ok, body, status = svc.get_run_detail("IDEA-009", rid, root=real_pair["root"])
            assert status == 200
            expected = compare.build_scope(real_pair["runs_dir"] / rid)
            assert body["run"]["scope"] == expected


class TestCompareDetailed:
    def test_comparison_has_exactly_the_contract_keys(self, real_pair):
        ok, body, status = _compare(real_pair)
        assert ok and status == 200
        assert body["comparison_note"] is None
        assert set(body["comparison"]) == set(_ts_keys("RunComparison"))
        json.dumps(body)

    def test_legacy_keys_are_unchanged(self, real_pair):
        _ok, body, _ = _compare(real_pair)
        for key in LEGACY_KEYS:
            assert key in body

    def test_legacy_pair_has_null_comparison_and_a_note(self, root):
        _ok, body, status = svc.compare_runs("IDEA-001", "2026-09-30-s-v1", "2026-10-05-s-v2", root)
        assert status == 200 and body["comparison"] is None
        assert (
            body["comparison_note"] == "Detailed comparison needs both runs to have a run folder."
        )

    def test_mixed_pair_has_null_comparison_and_a_note(self, real_pair):
        legacy = _run("2026-09-01-legacy-v1", "2026-09-01")
        real_pair["records"].append(legacy)
        real_pair["ids"].append(legacy["run_id"])
        _ok, body, status = _compare(real_pair, 0, 2)
        assert status == 200 and body["comparison"] is None
        assert (
            body["comparison_note"] == "Detailed comparison needs both runs to have a run folder."
        )

    def test_compare_error_gives_null_and_a_pathless_note(self, real_pair):
        folder = real_pair["runs_dir"] / real_pair["ids"][1]
        (folder / "evidence" / "trades.parquet").unlink()
        _ok, body, status = _compare(real_pair)
        assert status == 200 and body["comparison"] is None
        note = body["comparison_note"]
        assert isinstance(note, str) and note
        assert str(real_pair["root"]) not in note and "/" not in note

    def test_symlinked_run_folder_is_rejected_with_400(self, real_pair, tmp_path):
        folder = real_pair["runs_dir"] / real_pair["ids"][1]
        moved = tmp_path / "elsewhere" / folder.name
        moved.parent.mkdir()
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
        ok, _body, status = _compare(real_pair)
        assert status == 400 and not ok

    def test_mismatched_artifact_dir_is_rejected_with_400(self, real_pair):
        real_pair["records"][1] = {**real_pair["records"][1], "artifact_dir": "other/"}
        ok, _body, status = _compare(real_pair)
        assert status == 400 and not ok

    def test_unexpected_exception_is_not_a_500(self, real_pair, monkeypatch):
        from signal_engine.backtest import compare

        def boom(a, b):
            raise RuntimeError("secret /path/detail")

        monkeypatch.setattr(compare, "compare_run_folders", boom)
        ok, body, status = _compare(real_pair)
        assert ok and status == 200 and body["comparison"] is None
        assert body["comparison_note"] == "Detailed comparison is unavailable for these runs."


def test_importing_the_service_does_not_load_the_comparison_engine():
    code = (
        "import sys; sys.path.insert(0, '.');"
        "import services.backtest_results_service;"
        "bad=[m for m in sys.modules if m in ('scipy',"
        "'signal_engine.backtest.compare','signal_engine.backtest.harness')"
        " or m.startswith('scipy.')];"
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    repo = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        ["uv", "run", "python", "-c", code], cwd=repo, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stdout + out.stderr


FP = ["a", "b", "c"]
VERSION_KEYS = {"version", "strategy_fingerprint", "parent", "created_ist", "note"}


def _versions_yaml(rows):
    out = []
    for r in rows:
        out.append(f"- version: {r[0]}")
        out.append(f"  strategy_fingerprint: {r[1] * 64}")
        out.append(f"  parent: {r[2] if r[2] else 'null'}")
        out.append(f"  created_ist: '2026-10-0{len(out) % 9 + 1}T10:00:00+05:30'")
        out.append(f"  note: note {r[0]}")
    return "\n".join(out) + "\n"


THREE = [("v1", "a", None), ("v2", "b", "v1"), ("v2b", "c", "v1")]


def _idea_folder(root):
    return root / "research" / "ideas" / "idea-001-sample"


def _freeze_registry(monkeypatch, root, **pointer_overrides):
    """Serve the already-built registry, as when versions.yaml changes after it was cached."""
    ideas = svc.load_ideas(root)
    for idea in ideas:
        idea.update(pointer_overrides)
    monkeypatch.setattr(svc, "_load", lambda _r: ideas)


class TestStrategyVersions:
    def test_versions_in_file_order_with_exactly_five_keys(self, root):
        (_idea_folder(root) / "versions.yaml").write_text(_versions_yaml(THREE), encoding="utf-8")
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok
        assert [v["version"] for v in body["versions"]] == ["v1", "v2", "v2b"]
        assert all(set(v) == VERSION_KEYS for v in body["versions"])
        assert body["versions"][0]["parent"] is None
        assert body["versions"][2]["parent"] == "v1"
        assert all(type(v) is dict for v in body["versions"])
        json.dumps(body["versions"])

    def test_no_versions_file_is_null(self, root):
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok and body["versions"] is None

    def test_malformed_yaml_is_null_not_an_error(self, root, monkeypatch):
        _freeze_registry(monkeypatch, root)
        (_idea_folder(root) / "versions.yaml").write_text("- version: [unclosed", encoding="utf-8")
        ok, body, status = svc.get_idea_results("IDEA-001", root)
        assert ok and status == 200 and body["versions"] is None

    def test_invalid_content_is_null(self, root, monkeypatch):
        _freeze_registry(monkeypatch, root)
        (_idea_folder(root) / "versions.yaml").write_text("- version: nope\n", encoding="utf-8")
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok and body["versions"] is None

    def test_symlinked_versions_file_is_null(self, root, tmp_path_factory):
        target = tmp_path_factory.mktemp("elsewhere") / "v.yaml"
        target.write_text(_versions_yaml(THREE), encoding="utf-8")
        (_idea_folder(root) / "versions.yaml").symlink_to(target)
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok and body["versions"] is None

    def test_oversized_versions_file_is_null(self, root):
        text = _versions_yaml(THREE) + "# " + "x" * (3 * 1024 * 1024) + "\n"
        (_idea_folder(root) / "versions.yaml").write_text(text, encoding="utf-8")
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok and body["versions"] is None

    def test_pointers_absent_are_none(self, root):
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert body["version_pointers"] == {
            "current_version": None,
            "promoted_version": None,
            "promoted_run_id": None,
        }

    def test_pointers_present_pass_through(self, root):
        (_idea_folder(root) / "versions.yaml").write_text(_versions_yaml(THREE), encoding="utf-8")
        md = _idea_folder(root) / "idea.md"
        text = md.read_text(encoding="utf-8").replace(
            "strategy_tags: [vwap]\n",
            "strategy_tags: [vwap]\ncurrent_version: v2\npromoted_version: v1\n"
            "promoted_run_id: 2026-09-30-s-v1\n",
        )
        md.write_text(text, encoding="utf-8")
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert body["version_pointers"] == {
            "current_version": "v2",
            "promoted_version": "v1",
            "promoted_run_id": "2026-09-30-s-v1",
        }

    def test_non_string_pointer_is_none(self, root, monkeypatch):
        _freeze_registry(monkeypatch, root, current_version=3, promoted_version=["v1"])
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok and body["version_pointers"]["current_version"] is None
        assert body["version_pointers"]["promoted_version"] is None

    def test_run_strategy_version_passes_through(self, tmp_path):
        _add_idea(tmp_path, "001", runs=[_run(strategy_version="v2")])
        ok, body, _ = svc.get_idea_results("IDEA-001", tmp_path)
        assert body["runs"][0]["strategy_version"] == "v2"

    def test_legacy_idea_keeps_the_rest_of_the_response(self, root):
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok and body["versions"] is None
        assert len(body["runs"]) == 2 and body["spec"] is None
        assert body["research_notes"] == "Body."
        assert "strategy_version" not in body["runs"][0]


class TestVersionsError:
    def _error(self, root):
        ok, body, _ = svc.get_idea_results("IDEA-001", root)
        assert ok
        return body

    def test_absent_and_healthy_have_null_error(self, root):
        assert self._error(root)["versions_error"] is None
        (_idea_folder(root) / "versions.yaml").write_text(_versions_yaml(THREE), encoding="utf-8")
        body = self._error(root)
        assert body["versions_error"] is None and body["versions"] is not None

    def test_invalid_yaml_and_content(self, root, monkeypatch):
        _freeze_registry(monkeypatch, root)
        path = _idea_folder(root) / "versions.yaml"
        for text in ("- version: [unclosed", "- version: nope\n"):
            path.write_text(text, encoding="utf-8")
            body = self._error(root)
            assert body["versions"] is None
            assert body["versions_error"] == "versions.yaml is invalid"

    def test_symlink_is_not_readable(self, root, tmp_path_factory):
        target = tmp_path_factory.mktemp("elsewhere") / "v.yaml"
        target.write_text(_versions_yaml(THREE), encoding="utf-8")
        (_idea_folder(root) / "versions.yaml").symlink_to(target)
        body = self._error(root)
        assert body["versions"] is None
        assert body["versions_error"] == "versions.yaml is not readable"

    def test_oversized(self, root):
        text = _versions_yaml(THREE) + "# " + "x" * (3 * 1024 * 1024) + "\n"
        (_idea_folder(root) / "versions.yaml").write_text(text, encoding="utf-8")
        body = self._error(root)
        assert body["versions"] is None
        assert body["versions_error"] == "versions.yaml is larger than the allowed size"

    def test_unreadable_oserror(self, root, monkeypatch):
        (_idea_folder(root) / "versions.yaml").write_text(_versions_yaml(THREE), encoding="utf-8")

        def boom(path, base):
            raise PermissionError("denied")

        monkeypatch.setattr(svc, "_read_text", boom)
        assert self._error(root)["versions_error"] == "versions.yaml is not readable"

    def test_message_never_contains_a_path(self, root, tmp_path_factory, monkeypatch):
        _freeze_registry(monkeypatch, root)
        target = tmp_path_factory.mktemp("elsewhere") / "v.yaml"
        target.write_text("x", encoding="utf-8")
        path = _idea_folder(root) / "versions.yaml"
        path.symlink_to(target)
        msg = self._error(root)["versions_error"]
        assert "/" not in msg and str(root) not in msg and "elsewhere" not in msg

    def test_versions_file_is_opened_once(self, root, monkeypatch):
        _freeze_registry(monkeypatch, root)
        path = _idea_folder(root) / "versions.yaml"
        path.write_text(_versions_yaml(THREE), encoding="utf-8")
        opened = []
        real_open, real_read_text = Path.open, Path.read_text

        def counting_open(self, *a, **k):
            if self.name == "versions.yaml":
                opened.append("open")
            return real_open(self, *a, **k)

        def counting_read_text(self, *a, **k):
            if self.name == "versions.yaml":
                opened.append("read_text")
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "open", counting_open)
        monkeypatch.setattr(Path, "read_text", counting_read_text)
        body = self._error(root)
        assert [v["version"] for v in body["versions"]] == ["v1", "v2", "v2b"]
        assert opened == ["open"]


# ---- Evidence read-model: run detail extras, paged trades, trade lineage --------------------

import math  # noqa: E402

import pandas as pd  # noqa: E402

from signal_engine.backtest.trades_io import LEDGER_SCHEMAS  # noqa: E402

_COST_KEYS = ("brokerage", "stt", "exchange", "gst", "sebi", "stamp", "slippage")
_IST = "Asia/Kolkata"


def _trade_frame(with_lineage=True, with_costs=True):
    times = ["2026-01-05 09:30", "2026-02-10 10:00", "2026-03-02 09:45", "2026-04-07 11:00"]
    n = len(times)
    frame = pd.DataFrame(
        {
            "trade_id": [f"T-{i + 1:06d}" for i in range(n)],
            "symbol": ["AAA", "BBB", "AAA", "CCC"],
            "side": ["BUY"] * n,
            "entry_time": pd.to_datetime(times).tz_localize(_IST),
            "exit_time": pd.to_datetime(times).tz_localize(_IST) + pd.Timedelta(minutes=30),
            "entry_price": [100.0, 200.0, 300.0, 400.0],
            "exit_price": [101.0, 190.0, 310.0, 380.0],
            "quantity": [1, 2, 3, 4],
            "exit_reason": ["TP", "SL", "SL_GAP", "SL_GAP_ENTRY"],
            "gross_r": [1.0, -1.0, 0.5, float("nan")],
            "net_r": [0.8, -1.2, 0.3, -2.0],
            "gross_bps": [10.0, -50.0, 30.0, -500.0],
            "net_bps": [8.0, -52.0, 28.0, -502.0],
            "cost_bps": [2.0, 2.0, 2.0, 2.0],
        }
    )
    if with_lineage:
        frame["signal_id"] = [f"{s}|s{i}|1" for i, s in enumerate(frame["symbol"])]
        frame["order_id"] = ["O|" + s for s in frame["signal_id"]]
        frame["entry_fill_id"] = ["F|" + o + "|entry1" for o in frame["order_id"]]
        frame["exit_fill_id"] = ["F|" + o + "|exit1" for o in frame["order_id"]]
        frame["position_id"] = ["P|" + f for f in frame["entry_fill_id"]]
    if with_costs:
        for i, key in enumerate(_COST_KEYS):
            frame[f"cost_{key}_bps"] = [float(i + 1), float(i + 2), float(i + 3), float(i + 4)]
        frame["modeled_cost_bps"] = [10.0, 12.0, 14.0, 16.0]
    return frame


@pytest.fixture(autouse=True)
def _fresh_evidence_caches():
    svc._clear_evidence_caches()
    yield
    svc._clear_evidence_caches()


def _put_parquet(folder: Path, rel: str, frame: pd.DataFrame, schema=None):
    svc._clear_evidence_caches()
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if schema is not None:
        import pyarrow as pa

        pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
        table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
        import pyarrow.parquet as pq

        pq.write_table(table, path)
    else:
        frame.to_parquet(path, index=False)
    return path


def _ledgers(folder: Path, trades: pd.DataFrame):
    sig = pd.DataFrame(
        {
            "signal_id": trades["signal_id"],
            "symbol": trades["symbol"],
            "bar_time": trades["entry_time"],
            "direction": [1] * len(trades),
            "tag": ["t"] * len(trades),
            "signal_price": [1.0] * len(trades),
            "stop": [0.5] * len(trades),
            "target": [2.0] * len(trades),
            "risk": [0.5] * len(trades),
            "accepted": [True] * len(trades),
            "exclusion_id": [None] * len(trades),
        }
    )
    orders = pd.DataFrame(
        {
            "order_id": trades["order_id"],
            "signal_id": trades["signal_id"],
            "symbol": trades["symbol"],
            "submit_time": trades["entry_time"],
            "order_type": ["MARKET"] * len(trades),
            "side": ["BUY"] * len(trades),
            "fill_bar_time": trades["entry_time"],
            "status": ["FILLED"] * len(trades),
            "exclusion_id": [None] * len(trades),
        }
    )
    rows = []
    for _, t in trades.iterrows():
        # written out of order on purpose: exit leg 2 first
        rows.append((t["order_id"], t["exit_fill_id"], t["exit_time"], 2, 1.5))
        rows.append((t["order_id"], t["entry_fill_id"], t["entry_time"], 1, 1.0))
    fills = pd.DataFrame(
        {
            "fill_id": [r[1] for r in rows],
            "order_id": [r[0] for r in rows],
            "position_id": [trades["position_id"].iloc[0]] * len(rows),
            "symbol": ["X"] * len(rows),
            "fill_time": [r[2] for r in rows],
            "fill_price": [r[4] for r in rows],
            "side": ["BUY"] * len(rows),
            "kind": ["entry" if r[3] == 1 else "exit" for r in rows],
            "exit_reason": [None] * len(rows),
            "leg": [r[3] for r in rows],
        }
    )
    for name, frame in (("signals", sig), ("orders", orders), ("fills", fills)):
        _put_parquet(folder, f"evidence/{name}.parquet", frame, LEDGER_SCHEMAS[name])


@pytest.fixture
def ev(detail_root):
    """A schema 3 run with trades (lineage + costs) and ledgers, split at 2026-03-01."""
    detail_root["runs"] = [_flat(split={"type": "date", "oos_start": "2026-03-01"})]
    folder = _make_run_folder(detail_root["runs_dir"])
    trades = _trade_frame()
    _put_parquet(folder, "evidence/trades.parquet", trades)
    _ledgers(folder, trades)
    detail_root["folder"] = folder
    detail_root["trades"] = trades
    return detail_root


def _trades(state, run_id=RID, **kw):
    return svc.get_run_trades("IDEA-001", run_id, root=state["tmp"], **kw)


def _ids(result):
    return [r["trade_id"] for r in result[1]["page"]["rows"]]


class TestDetailEvidenceFields:
    def test_validity_trials_ledgers_from_report(self, ev):
        report = dict(
            REPORT,
            validity={
                "ok": False,
                "failures": [{"check": "c", "detail": "d"}],
                "checks_run": ["c"],
            },
            trials={"material": 2, "external": 1, "total": 3, "source": "trials.yaml"},
            ledgers={"signals": 4, "trades": 4},
        )
        _write(ev["folder"] / "report" / "report.json", report)
        run = _detail(ev)[1]["run"]
        assert run["validity"]["ok"] is False and run["validity"]["checks_run"] == ["c"]
        assert run["trials"]["total"] == 3
        assert run["ledgers"] == {"signals": 4, "trades": 4}

    def test_wrong_shapes_become_null(self, ev):
        report = dict(REPORT, validity=[1], trials="x", ledgers=5, data_quality=7)
        _write(ev["folder"] / "report" / "report.json", report)
        run = _detail(ev)[1]["run"]
        assert run["validity"] is None and run["trials"] is None
        assert run["ledgers"] is None and run["data_quality"] is None

    def test_data_quality_prefers_file_then_report(self, ev):
        _write(ev["folder"] / "report" / "report.json", dict(REPORT, data_quality={"status": "R"}))
        assert _detail(ev)[1]["run"]["data_quality"] == {"status": "R"}
        _write(ev["folder"] / "data_quality.json", {"status": "PASS", "items": []})
        assert _detail(ev)[1]["run"]["data_quality"]["status"] == "PASS"

    def test_data_quality_null_when_neither(self, ev):
        assert _detail(ev)[1]["run"]["data_quality"] is None

    def test_data_quality_symlink_rejected(self, ev):
        real = ev["tmp"] / "dq.json"
        real.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
        os.symlink(real, ev["folder"] / "data_quality.json")
        assert _detail(ev)[1]["run"]["data_quality"] is None

    def test_cost_components_are_hand_computed_means(self, ev):
        cc = _detail(ev)[1]["run"]["cost_components"]
        assert cc["trades"] == 4
        by_key = {c["key"]: c for c in cc["components"]}
        assert [c["key"] for c in cc["components"]] == list(_COST_KEYS)
        # brokerage column is 1,2,3,4 -> mean 2.5 ; stt is 2,3,4,5 -> 3.5
        assert by_key["brokerage"]["mean_bps"] == pytest.approx(2.5)
        assert by_key["stt"]["mean_bps"] == pytest.approx(3.5)
        assert by_key["stt"]["label"] == "STT"
        assert by_key["gst"]["label"] == "GST"
        assert by_key["sebi"]["label"] == "SEBI fee"
        assert by_key["stamp"]["label"] == "Stamp duty"
        assert by_key["exchange"]["label"] == "Exchange charges"
        assert cc["modeled_cost_bps_mean"] == pytest.approx(13.0)
        assert cc["engine_cost_bps_mean"] == pytest.approx(2.0)

    def test_cost_components_null_without_cost_columns(self, ev):
        _put_parquet(ev["folder"], "evidence/trades.parquet", _trade_frame(with_costs=False))
        run = _detail(ev)[1]["run"]
        assert run["cost_components"] is None
        assert run["has_trades"] is True

    def test_has_flags(self, ev):
        run = _detail(ev)[1]["run"]
        assert run["has_trades"] is True and run["has_ledgers"] is True

    def test_has_ledgers_false_without_ledger_files(self, ev):
        for name in ("signals", "orders", "fills"):
            (ev["folder"] / "evidence" / f"{name}.parquet").unlink()
        run = _detail(ev)[1]["run"]
        assert run["has_trades"] is True and run["has_ledgers"] is False

    def test_symlinked_trades_not_counted(self, ev):
        trades = ev["folder"] / "evidence" / "trades.parquet"
        moved = ev["tmp"] / "t.parquet"
        trades.rename(moved)
        os.symlink(moved, trades)
        run = _detail(ev)[1]["run"]
        assert run["has_trades"] is False and run["cost_components"] is None

    def test_partial_run_without_evidence(self, detail_root):
        detail_root["runs"] = [_flat()]
        _make_run_folder(detail_root["runs_dir"])
        run = _detail(detail_root)[1]["run"]
        assert run["validity"] is None and run["trials"] is None and run["ledgers"] is None
        assert run["cost_components"] is None
        assert run["has_trades"] is False and run["has_ledgers"] is False

    def test_corrupt_trades_parquet_does_not_break_detail(self, ev):
        (ev["folder"] / "evidence" / "trades.parquet").write_bytes(b"not parquet")
        ok, body, status = _detail(ev)
        assert status == 200 and body["run"]["cost_components"] is None

    def test_legacy_run_returns_nulls_and_false(self, detail_root):
        detail_root["runs"] = [{"run_id": RID, "schema": 2}]
        run = _detail(detail_root)[1]["run"]
        for key in ("validity", "data_quality", "trials", "ledgers", "cost_components"):
            assert run[key] is None
        assert run["has_trades"] is False and run["has_ledgers"] is False


class TestRunTrades:
    def test_all_subset_defaults_and_row_columns(self, ev):
        ok, body, status = _trades(ev)
        assert (ok, status, body["status"]) == (True, 200, "success")
        page = body["page"]
        assert page["total"] == 4 and page["limit"] == 100 and page["offset"] == 0
        assert page["subset"] == "all"
        assert _ids((ok, body, status)) == ["T-000001", "T-000002", "T-000003", "T-000004"]
        row = page["rows"][0]
        for col in ("signal_id", "order_id", "position_id", "cost_stt_bps", "modeled_cost_bps"):
            assert col in row
        json.dumps(body, allow_nan=False)

    def test_timestamps_are_iso_ist(self, ev):
        row = _trades(ev)[1]["page"]["rows"][0]
        assert row["entry_time"] == "2026-01-05T09:30:00+05:30"
        assert row["exit_time"] == "2026-01-05T10:00:00+05:30"

    def test_nan_becomes_none(self, ev):
        rows = _trades(ev)[1]["page"]["rows"]
        assert rows[3]["gross_r"] is None
        assert not any(isinstance(v, float) and math.isnan(v) for r in rows for v in r.values())

    def test_inf_becomes_none(self, ev):
        frame = _trade_frame()
        frame.loc[0, "net_bps"] = float("inf")
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        row = _trades(ev)[1]["page"]["rows"][0]
        assert row["net_bps"] is None

    def test_in_sample_and_oos_split(self, ev):
        assert _ids(_trades(ev, subset="in_sample")) == ["T-000001", "T-000002"]
        assert _ids(_trades(ev, subset="oos")) == ["T-000003", "T-000004"]
        assert _trades(ev, subset="oos")[1]["page"]["total"] == 2

    def test_split_boundary_day_is_oos(self, ev):
        frame = _trade_frame()
        frame.loc[2, "entry_time"] = pd.Timestamp("2026-03-01 00:00", tz=_IST)
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        assert "T-000003" in _ids(_trades(ev, subset="oos"))
        frame.loc[2, "entry_time"] = pd.Timestamp("2026-02-28 23:59", tz=_IST)
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        assert "T-000003" in _ids(_trades(ev, subset="in_sample"))

    @pytest.mark.parametrize("subset", ["oos", "in_sample"])
    def test_no_split_gives_empty_page_with_message(self, ev, subset):
        ev["runs"][0].pop("split")
        ok, body, status = _trades(ev, subset=subset)
        assert status == 200
        assert body["page"]["rows"] == [] and body["page"]["total"] == 0
        assert "split" in body["message"].lower()

    def test_all_works_without_split(self, ev):
        ev["runs"][0].pop("split")
        assert _trades(ev)[1]["page"]["total"] == 4

    def test_losers(self, ev):
        assert _ids(_trades(ev, subset="losers")) == ["T-000002", "T-000004"]

    def test_gap_exits(self, ev):
        assert _ids(_trades(ev, subset="gap_exits")) == ["T-000003", "T-000004"]

    def test_unknown_subset_400(self, ev):
        assert _trades(ev, subset="bogus")[2] == 400

    def test_symbol_and_reason_filters(self, ev):
        assert _ids(_trades(ev, symbol="AAA")) == ["T-000001", "T-000003"]
        assert _ids(_trades(ev, reason="SL")) == ["T-000002"]
        assert _ids(_trades(ev, symbol="AAA", reason="SL_GAP")) == ["T-000003"]
        assert _trades(ev, symbol="aaa")[1]["page"]["total"] == 0

    def test_filters_combine_with_subset(self, ev):
        assert _ids(_trades(ev, subset="oos", symbol="AAA")) == ["T-000003"]

    def test_sort_whitelist_and_direction(self, ev):
        assert _ids(_trades(ev, sort="net_r")) == ["T-000004", "T-000002", "T-000003", "T-000001"]
        assert _ids(_trades(ev, sort="net_r", desc=True))[0] == "T-000001"
        assert _ids(_trades(ev, sort="symbol", desc=True))[0] == "T-000004"

    @pytest.mark.parametrize("bad", ["bogus", "net_r; drop", "../x", "", "SIGNAL_ID "])
    def test_sort_outside_whitelist_400(self, ev, bad):
        assert _trades(ev, sort=bad)[2] == 400

    def test_sort_on_missing_column_400(self, ev):
        _put_parquet(ev["folder"], "evidence/trades.parquet", _trade_frame(with_costs=False))
        assert _trades(ev, sort="cost_stt_bps")[2] == 400

    def test_nan_sorts_last(self, ev):
        assert _ids(_trades(ev, sort="gross_r"))[-1] == "T-000004"
        assert _ids(_trades(ev, sort="gross_r", desc=True))[-1] == "T-000004"

    def test_paging_and_total(self, ev):
        ok, body, _ = _trades(ev, limit=3, offset=0)
        assert len(body["page"]["rows"]) == 3 and body["page"]["total"] == 4
        ok, body, _ = _trades(ev, limit=3, offset=3)
        assert _ids((ok, body, 200)) == ["T-000004"] and body["page"]["total"] == 4
        assert _trades(ev, offset=99)[1]["page"]["rows"] == []

    @pytest.mark.parametrize("bad", [0, -5, 501, 100000])
    def test_out_of_range_limit_400(self, ev, bad):
        assert _trades(ev, limit=bad)[2] == 400

    def test_limit_bounds_accepted(self, ev):
        assert _trades(ev, limit=1)[1]["page"]["limit"] == 1
        assert _trades(ev, limit=500)[1]["page"]["limit"] == 500

    def test_negative_offset_400(self, ev):
        assert _trades(ev, offset=-1)[2] == 400

    def test_large_file_pages_fast(self, ev):
        n = 12000
        base = _trade_frame().iloc[[0]]
        big = pd.concat([base] * n, ignore_index=True)
        big["trade_id"] = [f"T-{i + 1:06d}" for i in range(n)]
        _put_parquet(ev["folder"], "evidence/trades.parquet", big)
        page = _trades(ev, limit=500, offset=11800, sort="trade_id")[1]["page"]
        assert page["total"] == n and len(page["rows"]) == 200

    def test_missing_parquet_404(self, ev):
        (ev["folder"] / "evidence" / "trades.parquet").unlink()
        ok, body, status = _trades(ev)
        assert status == 404 and body["message"] == "no trades for this run"

    def test_unknown_idea_and_run_404(self, ev):
        assert svc.get_run_trades("IDEA-009", RID, root=ev["tmp"])[2] == 404
        assert _trades(ev, run_id="2026-01-01-nope-v1")[2] == 404

    @pytest.mark.parametrize("bad", ["../x", "a/b", "..", "2026-09-29-orb-v1/../x", ""])
    def test_run_id_traversal_400(self, ev, bad):
        assert _trades(ev, run_id=bad)[2] == 400

    def test_bad_idea_id_400(self, ev):
        assert svc.get_run_trades("../etc", RID, root=ev["tmp"])[2] == 400

    def test_legacy_run_has_no_trades(self, detail_root):
        detail_root["runs"] = [{"run_id": RID, "schema": 2}]
        assert _trades(detail_root)[2] == 404

    def test_symlinked_parquet_rejected(self, ev):
        trades = ev["folder"] / "evidence" / "trades.parquet"
        moved = ev["tmp"] / "t.parquet"
        trades.rename(moved)
        os.symlink(moved, trades)
        ok, body, status = _trades(ev)
        assert ok is False and status in (400, 404)

    def test_oversize_file_refused(self, ev, monkeypatch):
        monkeypatch.setattr(svc, "_MAX_PARQUET_BYTES", 10)
        ok, body, status = _trades(ev)
        assert ok is False and status == 413

    def test_corrupt_parquet_generic_error_no_path(self, ev):
        (ev["folder"] / "evidence" / "trades.parquet").write_bytes(b"garbage")
        ok, body, status = _trades(ev)
        assert ok is False and status == 500
        assert str(ev["tmp"]) not in json.dumps(body)

    def test_errors_never_leak_paths(self, ev, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError(str(ev["tmp"]))

        monkeypatch.setattr(svc, "_load_frame", boom)
        ok, body, status = _trades(ev)
        assert status == 500 and str(ev["tmp"]) not in json.dumps(body)


class TestTradeLineage:
    def _lineage(self, state, trade_id="T-000001", run_id=RID):
        return svc.get_trade_lineage("IDEA-001", run_id, trade_id, root=state["tmp"])

    def test_full_chain(self, ev):
        ok, body, status = self._lineage(ev)
        assert (ok, status) == (True, 200)
        lin = body["lineage"]
        t = ev["trades"].iloc[0]
        assert lin["trade_id"] == "T-000001"
        assert lin["signal"]["signal_id"] == t["signal_id"]
        assert lin["order"]["order_id"] == t["order_id"]
        assert lin["position_id"] == t["position_id"]
        assert lin["signal"]["bar_time"] == "2026-01-05T09:30:00+05:30"
        json.dumps(body, allow_nan=False)

    def test_fills_ordered_by_time_then_leg(self, ev):
        fills = self._lineage(ev)[1]["lineage"]["fills"]
        assert [f["leg"] for f in fills] == [1, 2]
        assert fills[0]["fill_time"] < fills[1]["fill_time"]
        assert {f["order_id"] for f in fills} == {ev["trades"].iloc[0]["order_id"]}

    def test_only_this_trades_rows_returned(self, ev):
        lin = self._lineage(ev, "T-000002")[1]["lineage"]
        assert lin["order"]["order_id"] == ev["trades"].iloc[1]["order_id"]
        assert len(lin["fills"]) == 2

    def test_uses_predicate_pushdown(self, ev, monkeypatch):
        seen = []
        real = svc.pq.read_table

        def spy(path, *a, **k):
            seen.append((Path(str(path)).name, k.get("filters")))
            return real(path, *a, **k)

        monkeypatch.setattr(svc.pq, "read_table", spy)
        self._lineage(ev)
        by_name = dict(seen)
        for name in ("signals.parquet", "orders.parquet", "fills.parquet"):
            assert by_name[name], name

    def test_without_ledgers_parts_empty(self, ev):
        for name in ("signals", "orders", "fills"):
            (ev["folder"] / "evidence" / f"{name}.parquet").unlink()
        lin = self._lineage(ev)[1]["lineage"]
        assert lin["signal"] is None and lin["order"] is None and lin["fills"] == []
        assert lin["position_id"] == ev["trades"].iloc[0]["position_id"]

    def test_without_lineage_columns(self, ev):
        _put_parquet(ev["folder"], "evidence/trades.parquet", _trade_frame(with_lineage=False))
        lin = self._lineage(ev)[1]["lineage"]
        assert lin["signal"] is None and lin["order"] is None
        assert lin["fills"] == [] and lin["position_id"] is None
        assert lin["missing"] == ["signal_id", "order_id", "position_id"]

    def test_unknown_trade_404(self, ev):
        assert self._lineage(ev, "T-999999")[2] == 404

    @pytest.mark.parametrize("bad", ["T-1", "x", "T-000001/..", "../T-000001", "t-000001", ""])
    def test_bad_trade_id_400(self, ev, bad):
        assert self._lineage(ev, bad)[2] == 400

    def test_trade_id_newline_rejected(self, ev):
        assert self._lineage(ev, "T-000001\n")[2] == 400

    def test_missing_trades_parquet_404(self, ev):
        (ev["folder"] / "evidence" / "trades.parquet").unlink()
        assert self._lineage(ev)[2] == 404

    def test_unknown_run_404_and_bad_run_400(self, ev):
        assert self._lineage(ev, run_id="2026-01-01-nope-v1")[2] == 404
        assert self._lineage(ev, run_id="../x")[2] == 400

    def test_symlinked_ledger_ignored(self, ev):
        fills = ev["folder"] / "evidence" / "fills.parquet"
        moved = ev["tmp"] / "f.parquet"
        fills.rename(moved)
        os.symlink(moved, fills)
        lin = self._lineage(ev)[1]["lineage"]
        assert lin["fills"] == []


class TestEvidenceGuards:
    def test_row_cap_refuses_trades_with_413(self, ev, monkeypatch):
        monkeypatch.setattr(svc, "_MAX_PARQUET_ROWS", 2)
        ok, body, status = _trades(ev)
        assert (ok, status) == (False, 413)
        assert "too large" in body["message"] and str(ev["tmp"]) not in json.dumps(body)

    def test_row_cap_nulls_cost_components_and_logs(self, ev, monkeypatch):
        monkeypatch.setattr(svc, "_MAX_PARQUET_ROWS", 2)
        assert _detail(ev)[1]["run"]["cost_components"] is None

    def test_row_cap_on_ledger_refuses_lineage(self, ev, monkeypatch):
        real = svc._open_guarded

        def guarded(path):
            if path.name == "trades.parquet":
                return real(path)
            monkeypatch.setattr(svc, "_MAX_PARQUET_ROWS", 1)
            return real(path)

        monkeypatch.setattr(svc, "_open_guarded", guarded)
        ok, body, status = svc.get_trade_lineage("IDEA-001", RID, "T-000001", root=ev["tmp"])
        assert status == 413 and body["message"] == "ledger too large for interactive lineage"

    def test_cost_components_cached_per_file_version(self, ev, monkeypatch):
        _detail(ev)
        calls = []
        real = svc._compute_cost_components
        monkeypatch.setattr(svc, "_compute_cost_components", lambda p: calls.append(1) or real(p))
        _detail(ev)
        assert calls == []

    def test_paging_decodes_the_file_once(self, ev, monkeypatch):
        _trades(ev, limit=2, offset=0)
        reads = []
        real = svc.pq.ParquetFile.read
        monkeypatch.setattr(
            svc.pq.ParquetFile, "read", lambda self, *a, **k: reads.append(1) or real(self, *a, **k)
        )
        _trades(ev, limit=2, offset=2)
        _trades(ev, limit=2, offset=0, sort="net_r")
        assert reads == []

    def test_cache_invalidated_when_file_changes(self, ev):
        assert _trades(ev)[1]["page"]["total"] == 4
        path = ev["folder"] / "evidence" / "trades.parquet"
        frame = _trade_frame().iloc[:2]
        frame.to_parquet(path, index=False)
        os.utime(path, ns=(1, 1))
        assert _trades(ev)[1]["page"]["total"] == 2

    def test_projection_reads_only_known_columns(self, ev):
        frame = _trade_frame()
        frame["secret_blob"] = "x"
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        assert "secret_blob" not in _trades(ev)[1]["page"]["rows"][0]
        assert _trades(ev, sort="secret_blob")[2] == 400

    def test_schema_validated_before_any_read(self, ev, monkeypatch):
        _put_parquet(
            ev["folder"], "evidence/trades.parquet", _trade_frame().drop(columns=["symbol"])
        )
        monkeypatch.setattr(svc, "_load_frame", lambda *a: pytest.fail("data was read"))
        assert _trades(ev, symbol="AAA")[2] == 400

    def test_subset_note_names_the_exclusions(self, ev):
        page = _trades(ev, subset="oos")[1]["page"]
        assert "oos_start" in page["subset_note"] and "purge" in page["subset_note"]
        assert "oos_start" in _trades(ev, subset="in_sample")[1]["page"]["subset_note"]

    def test_nat_entry_time_is_ignored_by_split_subsets(self, ev):
        frame = _trade_frame()
        frame.loc[1, "entry_time"] = pd.NaT
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        assert _ids(_trades(ev, subset="in_sample")) == ["T-000001"]
        assert _ids(_trades(ev, subset="oos")) == ["T-000003", "T-000004"]
        assert _trades(ev)[1]["page"]["rows"][-1]["entry_time"] is None

    def test_big_ints_become_strings_and_bytes_decode(self, ev):
        frame = _trade_frame()
        frame["quantity"] = [2**60, 5, -(2**60), 2**53 - 1]
        frame["side"] = [b"BUY", b"SELL", b"\xff", b"x"]
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        rows = _trades(ev, sort="trade_id")[1]["page"]["rows"]
        assert rows[0]["quantity"] == str(2**60) and rows[2]["quantity"] == str(-(2**60))
        assert rows[1]["quantity"] == 5 and rows[3]["quantity"] == 2**53 - 1
        assert rows[0]["side"] == "BUY"
        assert not any(str(v).startswith("b'") for r in rows for v in r.values())

    def test_symlinked_ledger_still_ignored_with_guard(self, ev):
        assert svc._evidence_file(ev["folder"], "nope") is None


class TestLineageIntegrity:
    def _lineage(self, state, trade_id="T-000001"):
        return svc.get_trade_lineage("IDEA-001", RID, trade_id, root=state["tmp"])

    def test_duplicate_trade_id_409(self, ev):
        frame = pd.concat([_trade_frame(), _trade_frame().iloc[[0]]], ignore_index=True)
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        ok, body, status = self._lineage(ev)
        assert status == 409 and body["message"] == "ledger inconsistent"

    def test_duplicate_signal_row_409(self, ev):
        sigs = pd.read_parquet(ev["folder"] / "evidence" / "signals.parquet")
        sigs = pd.concat([sigs, sigs.iloc[[0]]], ignore_index=True)
        _put_parquet(ev["folder"], "evidence/signals.parquet", sigs, LEDGER_SCHEMAS["signals"])
        assert self._lineage(ev)[2] == 409

    def test_duplicate_order_row_409(self, ev):
        orders = pd.read_parquet(ev["folder"] / "evidence" / "orders.parquet")
        orders = pd.concat([orders, orders.iloc[[0]]], ignore_index=True)
        _put_parquet(ev["folder"], "evidence/orders.parquet", orders, LEDGER_SCHEMAS["orders"])
        assert self._lineage(ev)[2] == 409

    def test_ledger_missing_filter_column_422(self, ev):
        _put_parquet(ev["folder"], "evidence/signals.parquet", pd.DataFrame({"other": ["a"]}))
        ok, body, status = self._lineage(ev)
        assert status == 422 and body["message"] == "ledger schema unexpected"

    def test_trades_missing_trade_id_422(self, ev):
        _put_parquet(
            ev["folder"], "evidence/trades.parquet", _trade_frame().drop(columns=["trade_id"])
        )
        assert self._lineage(ev)[2] == 422

    def test_partial_lineage_columns_listed_as_missing(self, ev):
        frame = _trade_frame().drop(columns=["position_id"])
        _put_parquet(ev["folder"], "evidence/trades.parquet", frame)
        lin = self._lineage(ev)[1]["lineage"]
        assert lin["missing"] == ["position_id"] and lin["signal"] is not None

    def test_complete_run_reports_nothing_missing(self, ev):
        assert self._lineage(ev)[1]["lineage"]["missing"] == []

    def test_ledger_lookups_are_memoised(self, ev, monkeypatch):
        self._lineage(ev)
        monkeypatch.setattr(svc.pq, "read_table", lambda *a, **k: pytest.fail("re-read"))
        assert self._lineage(ev)[2] == 200
