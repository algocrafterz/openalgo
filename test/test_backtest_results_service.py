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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import backtest_results_service as svc  # noqa: E402

IDEA_MD = """---
id: IDEA-{num}
title: Sample idea {num}
status: rejected
verdict: no-edge
category: intraday-momentum
horizon: intraday
source:
  type: video
  ref: "https://example.invalid/v"
summary: One line of what the idea is.
result: Lost money before costs.
revisit_when: Never.
last_reviewed: 2026-09-29
tags: [vwap]
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
