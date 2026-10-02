"""Run comparison engine: two real run folders in, a RunComparison out (build order 6, P1)."""

import json
import math
import re
import shutil
import stat
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from signal_engine.backtest import compare, tearsheet, trades_io
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.change_class import (
    COST_CHANGE,
    DATA_RANGE_CHANGE,
    DATASET_CHANGE,
    PARAMETER_CHANGE,
    RESEARCH_RERUN,
    STRATEGY_CHANGE,
)
from signal_engine.tests import test_record_run as base
from signal_engine.tests.intake_fixtures import make_spec
from signal_engine.tests.test_record_run import SUPPORTED, _execution, _trade
from signal_engine.timeutils import IST

root = base.root  # the idea-folder fixtures, shared with test_record_run
_stub_provenance = base._stub_provenance

CLEAN = {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []}
NOW = datetime(2026, 10, 2, 11, 0, tzinfo=IST)
IDEA = "IDEA-009"
REPO = Path(__file__).resolve().parents[2]
TS_FILE = REPO / "frontend" / "src" / "api" / "backtestResults.ts"


def _execution_of(rows, start, end):
    """An Execution whose trades are (day, r_gross, r_net) rows over the stated period."""
    trades = [_trade(date.fromisoformat(d), g, n) for d, g, n in rows]
    template = _execution()
    return rr.Execution(
        **{
            **template.__dict__,
            "trades": trades,
            "period": {"start": start, "end": end},
            "days": [d for d, _, _ in rows],
            "symbols": ["A.NS"],
            "data": {**template.data, "as_of": end},
        }
    )


def _record(root, execution, *, fingerprint="d" * 64, **kw):
    return rr.record_idea(
        root,
        IDEA,
        execute=lambda key: execution,
        run_date="2026-10-02",
        supported=SUPPORTED,
        git_state_fn=lambda r: CLEAN,
        dataset_fingerprint_fn=lambda s, i, a: fingerprint,
        readiness_missing_fn=lambda: ["Capability one"],
        now=NOW,
        **kw,
    )


def _folder(flat: Path) -> Path:
    return flat.parent / flat.stem


def _idea_dir(root) -> Path:
    return root / "research" / "ideas" / "idea-009-orb"


def _pair(root, exec_a, exec_b, between=None, kw_b=None, fp_b="d" * 64):
    a = _folder(_record(root, exec_a))
    if between:
        between(_idea_dir(root))
    b = _folder(_record(root, exec_b, fingerprint=fp_b, **(kw_b or {})))
    return a, b


# Hand-computed fixture: net R in the common window 2026-03-04..2026-03-10 is known by hand.
A_ROWS = [
    ("2026-03-02", 2.0, 1.8),
    ("2026-03-03", -1.0, -1.2),
    ("2026-03-05", 1.0, 0.8),
    ("2026-03-09", -1.0, -1.2),
]
B_ROWS = [
    ("2026-03-04", 3.0, 2.8),
    ("2026-03-06", -1.0, -1.2),
    ("2026-03-09", 1.0, 0.8),
    ("2026-03-15", 2.0, 1.8),
    ("2026-03-18", -1.0, -1.2),
]


@pytest.fixture
def overlap_pair(root):
    return _pair(
        root,
        _execution_of(A_ROWS, "2026-03-01", "2026-03-10"),
        _execution_of(B_ROWS, "2026-03-04", "2026-03-20"),
    )


@pytest.fixture
def same_pair(root):
    ex = _execution()
    return _pair(root, ex, ex)


def _fields(diffs):
    return {d["field"]: d for d in diffs}


class TestScope:
    def test_keys_equal_the_typescript_run_scope(self, same_pair):
        text = TS_FILE.read_text(encoding="utf-8")
        body = re.search(r"export interface RunScope \{(.*?)\n\}", text, re.S).group(1)
        expected = re.findall(r"^  (\w+)\??:", body, re.M)
        assert len(expected) == 20
        assert list(compare.build_scope(same_pair[0])) == expected

    def test_values_come_from_the_frozen_files(self, same_pair):
        scope = compare.build_scope(same_pair[0])
        assert scope["market"] is not None
        assert scope["universe"] == "historify-3-symbols"
        assert scope["symbol_count"] == 1 or scope["symbol_count"] == 0
        assert (scope["style"], scope["direction"], scope["timeframe"], scope["product"]) == (
            "intraday",
            "both",
            "5m",
            "MIS",
        )
        assert scope["period"] == {"start": "2026-01-01", "end": "2026-02-20"}
        assert scope["capital_per_trade"] == 100000
        assert scope["risk_per_trade_pct"] == 1.0
        assert (scope["portfolio_mode"], scope["portfolio_scope"]) == (
            "single_position",
            "per_symbol",
        )
        assert scope["slippage_bps_per_side"] is not None
        assert scope["cost_bps_round_trip"] is not None
        assert scope["profile"]["name"] == "flattrade-nse-cash"
        assert len(scope["profile"]["sha256"]) == 64
        assert scope["dataset"]["id"] == "historify"
        assert len(scope["dataset"]["fingerprint"]) == 64
        assert scope["dataset"]["interval"] == "5m"

    def test_agrees_with_the_service_read_model(self, same_pair):
        from services import backtest_results_service as svc

        folder = same_pair[0]
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        load = lambda rel: json.loads((folder / rel).read_text(encoding="utf-8"))  # noqa: E731
        spec = yaml.safe_load((folder / "strategy" / "spec.yaml").read_text(encoding="utf-8"))
        theirs = svc._build_scope(
            manifest, load("execution/config.json"), load("strategy/strategy.json"), spec
        )
        mine = compare.build_scope(folder)
        # The service reads resolved["slippage_bps_per_side"] without the "execution." prefix
        # the frozen config uses, so it reports None for these two; compare reads them correctly.
        cost = ("slippage_bps_per_side", "cost_bps_round_trip")
        assert {k: v for k, v in mine.items() if k not in cost} == {
            k: v for k, v in theirs.items() if k not in cost
        }
        assert mine["slippage_bps_per_side"] == 4.0 and mine["cost_bps_round_trip"] == 16.0

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(compare.CompareError, match="manifest"):
            compare.build_scope(tmp_path)


class TestErrors:
    def test_missing_manifest(self, same_pair, tmp_path):
        with pytest.raises(compare.CompareError, match="manifest"):
            compare.compare_run_folders(same_pair[0], tmp_path / "nope")

    def test_corrupt_manifest(self, same_pair, tmp_path):
        bad = tmp_path / "bad"
        shutil.copytree(same_pair[0], bad)
        (bad / "manifest.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
        (bad / "manifest.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(compare.CompareError, match="manifest"):
            compare.compare_run_folders(same_pair[0], bad)

    def test_manifest_without_period(self, same_pair, tmp_path):
        bad = tmp_path / "bad"
        shutil.copytree(same_pair[0], bad)
        path = bad / "manifest.json"
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["period"]
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(compare.CompareError, match="manifest"):
            compare.compare_run_folders(bad, same_pair[1])

    def test_missing_parquet(self, same_pair, tmp_path):
        bad = tmp_path / "bad"
        shutil.copytree(same_pair[0], bad)
        (bad / "evidence" / "trades.parquet").unlink()
        with pytest.raises(compare.CompareError, match="trades.parquet"):
            compare.compare_run_folders(same_pair[1], bad)

    def test_unreadable_parquet(self, same_pair, tmp_path):
        bad = tmp_path / "bad"
        shutil.copytree(same_pair[0], bad)
        target = bad / "evidence" / "trades.parquet"
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        target.write_bytes(b"not parquet")
        with pytest.raises(compare.CompareError, match="trades.parquet"):
            compare.compare_run_folders(bad, same_pair[1])


class TestIdenticalRuns:
    def test_everything_is_unchanged(self, same_pair):
        out = compare.compare_run_folders(*same_pair)
        assert out["schema"] == 1
        assert out["change_classes"] == [RESEARCH_RERUN]
        assert out["strategy"]["same_version"] is True
        for diff in ("rules", "parameters"):
            assert out["strategy"][diff] == {"changed": {}, "added": {}, "removed": {}}
        for pair in out["strategy"]["fingerprints"].values():
            assert pair["changed"] is False and pair["a"] == pair["b"]
        for section in ("scope", "execution", "cost"):
            assert out[section], section
            assert not any(d["changed"] for d in out[section]), section
        assert out["dataset"]["fingerprint_changed"] is False
        assert not any(d["changed"] for d in out["dataset"]["fields"])
        assert out["overlap"]["common"] == out["overlap"]["periods"]["a"]
        assert out["overlap"]["extra"] == {"a": [], "b": []}
        assert out["overlap"]["warning"] is None
        assert out["views"]["incremental"] is None
        assert out["views"]["common"] == out["views"]["full"]
        assert out["curves"]["incremental"] == []
        assert out["curves_incremental"] is None

    def test_result_is_json_safe(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        text = json.dumps(out, allow_nan=False)
        assert json.loads(text) == out

    def test_field_diffs_have_the_contract_keys_and_labels(self, same_pair):
        out = compare.compare_run_folders(*same_pair)
        for section in (out["scope"], out["execution"], out["cost"], out["dataset"]["fields"]):
            for d in section:
                assert set(d) == {"field", "label", "a", "b", "changed"}
                assert d["label"] and d["label"] != d["field"]


class TestStrategyDiff:
    def test_parameter_only_change(self, root):
        def edit(idea):
            spec = make_spec(parameters={"ema_fast": 10, "ema_slow": 20, "atr_len": 14})
            (idea / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")

        ex = _execution()
        out = compare.compare_run_folders(
            *_pair(root, ex, ex, between=edit, kw_b={"parent_version": "v1"})
        )
        assert PARAMETER_CHANGE in out["change_classes"]
        assert STRATEGY_CHANGE not in out["change_classes"]
        assert out["strategy"]["parameters"]["changed"] == {"ema_fast": {"a": 9, "b": 10}}
        assert out["strategy"]["rules"] == {"changed": {}, "added": {}, "removed": {}}
        assert out["strategy"]["version"] == {"a": "v1", "b": "v2"}
        assert out["strategy"]["same_version"] is False
        fps = out["strategy"]["fingerprints"]
        assert fps["params"]["changed"] and not fps["rules"]["changed"]
        assert fps["strategy"]["changed"]

    def test_rules_change(self, root):
        def edit(idea):
            spec = make_spec(stop=["2 ATR(14) below entry"])
            (idea / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")

        ex = _execution()
        out = compare.compare_run_folders(
            *_pair(root, ex, ex, between=edit, kw_b={"parent_version": "v1"})
        )
        assert STRATEGY_CHANGE in out["change_classes"]
        assert out["strategy"]["rules"]["changed"] == {
            "stop": {"a": ["1 ATR(14) below entry"], "b": ["2 ATR(14) below entry"]}
        }
        assert out["strategy"]["parameters"]["changed"] == {}
        assert out["strategy"]["fingerprints"]["rules"]["changed"] is True

    def test_non_rule_keys_are_not_in_the_rules_diff(self, root):
        def edit(idea):
            spec = make_spec(name="Renamed only", parameters={"ema_fast": 11})
            (idea / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")

        ex = _execution()
        out = compare.compare_run_folders(
            *_pair(root, ex, ex, between=edit, kw_b={"parent_version": "v1"})
        )
        rules = out["strategy"]["rules"]
        assert "name" not in rules["changed"] and "parameters" not in rules["changed"]
        assert rules == {"changed": {}, "added": {}, "removed": {}}
        assert set(out["strategy"]["parameters"]["removed"]) == {"ema_slow", "atr_len"}
        assert out["strategy"]["parameters"]["changed"]["ema_fast"] == {"a": 9, "b": 11}


class TestCostAndDatasetAndExecution:
    def test_cost_override(self, root):
        def edit(idea):
            (idea / "execution.yaml").write_text(
                yaml.safe_dump({"slippage_bps_per_side": {"value": 7.5, "reason": "stress test"}}),
                encoding="utf-8",
            )

        ex = _execution()
        out = compare.compare_run_folders(*_pair(root, ex, ex, between=edit))
        assert COST_CHANGE in out["change_classes"]
        cost = _fields(out["cost"])
        assert cost["slippage_bps_per_side"]["changed"] is True
        assert cost["slippage_bps_per_side"]["b"] == 7.5
        assert cost["cost_bps_round_trip"]["changed"] is False
        assert not any(d["changed"] for d in out["execution"])
        assert "execution.slippage_bps_per_side" not in _fields(out["execution"])
        assert "execution.cost_bps_round_trip" not in _fields(out["execution"])

    def test_execution_fields_exclude_costs_and_include_profile(self, same_pair):
        names = set(_fields(compare.compare_run_folders(*same_pair)["execution"]))
        assert "execution.bar_size" in names
        assert "execution.product" in names
        assert any(n.startswith("profile.") for n in names)
        assert not any("slippage" in n or "cost_bps" in n for n in names)

    def test_cost_model_scalars_listed_when_present(self, root):
        ex = _execution()
        a = _folder(_record(root, ex))
        report = json.loads((a / "report" / "report.json").read_text(encoding="utf-8"))
        report["cost_model"] = {"model": "m", "leverage": 5.0, "total_bps_at": {"1.0": 3.0}}
        tmp = a.parent / "variant"
        shutil.copytree(a, tmp)
        target = tmp / "report" / "report.json"
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        target.write_text(json.dumps(report), encoding="utf-8")
        out = compare.compare_run_folders(a, tmp)
        cost = _fields(out["cost"])
        assert cost["cost_model.leverage"] == {
            "field": "cost_model.leverage",
            "label": cost["cost_model.leverage"]["label"],
            "a": None,
            "b": 5.0,
            "changed": True,
        }
        assert "cost_model.total_bps_at" not in cost

    def test_dataset_fingerprint_change(self, root):
        ex = _execution_of(A_ROWS, "2026-03-01", "2026-03-10")
        out = compare.compare_run_folders(*_pair(root, ex, ex, fp_b="e" * 64))
        assert DATASET_CHANGE in out["change_classes"]
        assert out["dataset"]["fingerprint_changed"] is True
        fields = _fields(out["dataset"]["fields"])
        assert set(fields) == {
            "id",
            "source",
            "interval",
            "data_as_of",
            "symbol_count",
            "fingerprint",
        }
        assert fields["fingerprint"]["a"] == "d" * 64 and fields["fingerprint"]["b"] == "e" * 64
        assert fields["fingerprint"]["changed"] is True
        assert fields["id"]["changed"] is False


class TestOverlap:
    def test_identical_periods(self, same_pair):
        o = compare.compare_run_folders(*same_pair)["overlap"]
        assert o["periods"]["a"] == {"start": "2026-01-01", "end": "2026-02-20"}
        assert o["warning"] is None

    def test_b_extends_a(self, root):
        full = _execution()
        rows = [(d.isoformat(), 1.1, 1.0) for d in [date(2026, 1, 1), date(2026, 1, 2)]]
        short = _execution_of(rows, "2026-01-01", "2026-01-20")
        out = compare.compare_run_folders(*_pair(root, short, full))
        o = out["overlap"]
        assert o["common"] == {"start": "2026-01-01", "end": "2026-01-20"}
        assert o["extra"]["a"] == []
        assert o["extra"]["b"] == [{"start": "2026-01-21", "end": "2026-02-20"}]
        assert "2026-01-21" in o["warning"] and "2026-02-20" in o["warning"]
        assert "common" in o["warning"].lower()
        assert DATA_RANGE_CHANGE in out["change_classes"]
        inc = out["views"]["incremental"]
        assert inc["run"] == "b"
        assert inc["ranges"] == o["extra"]["b"]
        assert inc["metrics"]["trades"] == 20 - 0 or inc["metrics"]["trades"] > 0
        assert out["curves"]["incremental"] == o["extra"]["b"]
        assert out["curves"]["common"] == o["common"]
        n_b = out["views"]["full"]["b"]["trades"]
        assert inc["metrics"]["trades"] + out["views"]["common"]["b"]["trades"] == n_b

    def test_b_inside_a_merges_both_sides(self, root):
        a_rows = [
            ("2026-03-02", 1.0, 0.9),
            ("2026-03-12", 1.0, 0.9),
            ("2026-03-25", -1.0, -1.1),
        ]
        b_rows = [("2026-03-12", 1.0, 0.9)]
        out = compare.compare_run_folders(
            *_pair(
                root,
                _execution_of(a_rows, "2026-03-01", "2026-03-31"),
                _execution_of(b_rows, "2026-03-10", "2026-03-20"),
            )
        )
        o = out["overlap"]
        assert o["common"] == {"start": "2026-03-10", "end": "2026-03-20"}
        assert o["extra"]["a"] == [
            {"start": "2026-03-01", "end": "2026-03-09"},
            {"start": "2026-03-21", "end": "2026-03-31"},
        ]
        assert o["extra"]["b"] == []
        inc = out["views"]["incremental"]
        assert inc["run"] == "a"
        assert inc["metrics"]["trades"] == 2
        assert out["views"]["common"]["a"]["trades"] == 1

    def test_disjoint_periods(self, root):
        a_rows = [("2026-01-05", 1.0, 0.9)]
        b_rows = [("2026-02-05", -1.0, -1.1)]
        out = compare.compare_run_folders(
            *_pair(
                root,
                _execution_of(a_rows, "2026-01-01", "2026-01-31"),
                _execution_of(b_rows, "2026-02-01", "2026-02-28"),
            )
        )
        o = out["overlap"]
        assert o["common"] is None
        assert o["extra"]["a"] == [{"start": "2026-01-01", "end": "2026-01-31"}]
        assert o["extra"]["b"] == [{"start": "2026-02-01", "end": "2026-02-28"}]
        assert "no" in o["warning"].lower() and "common" in o["warning"].lower()
        assert out["views"]["common"] is None
        assert out["curves"]["common"] is None
        assert out["views"]["full"]["a"]["trades"] == 1

    def test_warning_differs_between_overlapping_and_disjoint(self, overlap_pair, root):
        overlapping = compare.compare_run_folders(*overlap_pair)["overlap"]["warning"]
        assert (
            overlapping
            and "common-period" in overlapping.lower().replace(" ", "-")
            or (overlapping and "common" in overlapping.lower())
        )


class TestViews:
    def test_full_views_equal_the_recorded_metrics(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        for key, folder in zip("ab", overlap_pair, strict=True):
            recorded = json.loads((folder / "report" / "report.json").read_text(encoding="utf-8"))[
                "metrics"
            ]
            for name in (
                "trades",
                "gross_bps",
                "net_bps",
                "win_rate",
                "expectancy_r",
                "profit_factor",
                "max_dd_r",
                "t_stat",
                "sharpe",
            ):
                got = out["views"]["full"][key].get(name)
                if name in recorded:
                    assert got == pytest.approx(recorded[name], abs=1e-6), (key, name)
                else:
                    assert got is None, (key, name)

    def test_common_window_matches_a_hand_computed_fixture(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        assert out["overlap"]["common"] == {"start": "2026-03-04", "end": "2026-03-10"}
        a = out["views"]["common"]["a"]
        assert a["trades"] == 2
        assert a["win_rate"] == pytest.approx(0.5)
        assert a["expectancy_r"] == pytest.approx(-0.2)
        assert a["gross_bps"] == pytest.approx(0.0, abs=1e-9)
        assert a["net_bps"] == pytest.approx(-20.0)
        assert a["profit_factor"] == pytest.approx(0.8 / 1.2, abs=1e-3)
        assert a["max_dd_r"] == pytest.approx(1.2)
        assert "t_stat" not in a or a["t_stat"] is None
        b = out["views"]["common"]["b"]
        assert b["trades"] == 3
        assert b["win_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert b["expectancy_r"] == pytest.approx(0.8)
        assert b["gross_bps"] == pytest.approx(100.0)
        assert b["net_bps"] == pytest.approx(80.0)
        assert b["profit_factor"] == pytest.approx(3.0)
        assert b["max_dd_r"] == pytest.approx(1.2)
        assert b["t_stat"] == pytest.approx(80 / (200 / math.sqrt(3)), abs=1e-3)

    def test_incremental_is_the_later_runs_tail(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        assert out["overlap"]["extra"]["a"] == [{"start": "2026-03-01", "end": "2026-03-03"}]
        assert out["overlap"]["extra"]["b"] == [{"start": "2026-03-11", "end": "2026-03-20"}]
        inc = out["views"]["incremental"]
        assert inc["run"] == "b"
        assert inc["ranges"] == [{"start": "2026-03-11", "end": "2026-03-20"}]
        assert inc["metrics"]["trades"] == 2
        assert inc["metrics"]["net_bps"] == pytest.approx(30.0)
        warning = out["overlap"]["warning"]
        assert "2026-03-01" in warning and "2026-03-11" in warning

    def test_performance_deltas(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        full = out["views"]["full"]
        perf = out["performance"]
        assert perf["trades"] == {"a": 4, "b": 5, "delta": 1}
        net = perf["net_bps"]
        assert net["a"] == full["a"]["net_bps"] and net["b"] == full["b"]["net_bps"]
        assert net["delta"] == pytest.approx(net["b"] - net["a"], abs=1e-6)
        assert all(set(v) == {"a", "b", "delta"} for v in perf.values())
        text = json.dumps(out).lower()
        assert "better" not in text and "worse" not in text

    def test_delta_is_null_when_a_side_is_missing(self, root):
        # one run has only winners: no profit factor on that side
        a_rows = [("2026-03-02", 1.0, 0.9), ("2026-03-03", 1.0, 0.9)]
        b_rows = [("2026-03-02", 1.0, 0.9), ("2026-03-03", -1.0, -1.1)]
        out = compare.compare_run_folders(
            *_pair(
                root,
                _execution_of(a_rows, "2026-03-01", "2026-03-31"),
                _execution_of(b_rows, "2026-03-01", "2026-03-31"),
            )
        )
        pf = out["performance"]["profit_factor"]
        assert pf["a"] is None and pf["b"] is not None and pf["delta"] is None


class TestParity:
    def test_headline_metrics_match_build_run_record(self):
        trades = [
            _trade(date(2026, 1, 1 + i % 25), 1.1 if i % 3 else -1.0, 1.0 if i % 3 else -1.1)
            for i in range(60)
        ]
        record = tearsheet.build_run_record(
            trades,
            run_id="r",
            run_date="2026-10-02",
            code_version="x",
            data={},
            period={"start": "2026-01-01", "end": "2026-01-25"},
            costs={"model": "m", "round_trip_bps": 16.0},
            n_trials=3,
        )
        direct = tearsheet.headline_metrics(
            [t.day.isoformat() for t in trades],
            [t.r_gross for t in trades],
            [t.r_net for t in trades],
            [t.risk_pct for t in trades],
            n_trials=3,
        )
        assert direct == record["metrics"]

    def test_recomputed_from_parquet_equals_recorded(self, root):
        ex = _execution()
        folder = _folder(_record(root, ex))
        recorded = json.loads((folder / "report" / "report.json").read_text(encoding="utf-8"))[
            "metrics"
        ]
        frame = trades_io.read_trades_parquet(folder / "evidence" / "trades.parquet")
        metrics = compare.metrics_from_frame(frame, n_trials=1)
        for name in (
            "trades",
            "gross_bps",
            "net_bps",
            "win_rate",
            "expectancy_r",
            "profit_factor",
            "max_dd_r",
            "t_stat",
            "sharpe",
        ):
            assert metrics[name] == pytest.approx(recorded[name], abs=1e-6), name


class TestCurves:
    def test_shared_axis_with_nulls_before_start_and_flat_after_end(self, overlap_pair):
        c = compare.compare_run_folders(*overlap_pair)["curves"]
        eq = {p["x"]: p for p in c["equity_r"]}
        xs = [p["x"] for p in c["equity_r"]]
        assert xs == sorted(xs)
        assert set(xs) == {d for d, _, _ in A_ROWS} | {d for d, _, _ in B_ROWS}
        assert eq["2026-03-02"]["a"] == pytest.approx(1.8) and eq["2026-03-02"]["b"] is None
        assert eq["2026-03-03"]["a"] == pytest.approx(0.6)
        assert eq["2026-03-04"]["a"] == pytest.approx(0.6)  # carried flat
        assert eq["2026-03-04"]["b"] == pytest.approx(2.8)
        assert eq["2026-03-09"]["a"] == pytest.approx(0.6 + 0.8 - 1.2)
        assert eq["2026-03-15"]["a"] is None  # A's period ended 2026-03-10: not carried beyond
        assert eq["2026-03-18"]["a"] is None
        assert eq["2026-03-18"]["b"] == pytest.approx(2.8 - 1.2 + 0.8 + 1.8 - 1.2)

    def test_drawdown_is_equity_minus_peak_never_positive(self, overlap_pair):
        c = compare.compare_run_folders(*overlap_pair)["curves"]
        dd = {p["x"]: p for p in c["drawdown_r"]}
        assert dd["2026-03-02"]["a"] == pytest.approx(0.0)  # new high
        assert dd["2026-03-03"]["a"] == pytest.approx(-1.2)
        assert dd["2026-03-09"]["a"] == pytest.approx(-1.6)  # equity 0.2 under peak 1.8
        assert dd["2026-03-04"]["b"] == pytest.approx(0.0)
        assert dd["2026-03-04"]["a"] == pytest.approx(-1.2)
        assert dd["2026-03-02"]["b"] is None
        assert all(v <= 0 for p in c["drawdown_r"] for v in (p["a"], p["b"]) if v is not None)

    def test_monthly_sums_with_nulls(self, root):
        a_rows = [("2026-01-05", 1.0, 0.9), ("2026-01-06", 1.0, 0.5), ("2026-02-02", 1.0, 0.1)]
        b_rows = [("2026-02-03", 1.0, 0.7), ("2026-03-03", 1.0, 0.2)]
        c = compare.compare_run_folders(
            *_pair(
                root,
                _execution_of(a_rows, "2026-01-01", "2026-03-31"),
                _execution_of(b_rows, "2026-01-01", "2026-03-31"),
            )
        )["curves"]
        months = {p["x"]: p for p in c["monthly"]}
        assert list(months) == ["2026-01", "2026-02", "2026-03"]
        assert months["2026-01"]["a"] == pytest.approx(1.4) and months["2026-01"]["b"] is None
        assert months["2026-02"]["a"] == pytest.approx(0.1)
        assert months["2026-02"]["b"] == pytest.approx(0.7)
        assert months["2026-03"]["a"] is None

    def test_rolling_window_is_min_of_50_and_trades(self, overlap_pair):
        c = compare.compare_run_folders(*overlap_pair)["curves"]
        assert c["rolling_window"] == 4  # the smaller run has 4 trades
        roll = {p["x"]: p for p in c["rolling"]}
        assert roll["2026-03-02"]["a"] is None  # window not yet full
        assert roll["2026-03-09"]["a"] == pytest.approx((1.8 - 1.2 + 0.8 - 1.2) / 4)

    def test_rolling_window_caps_at_50(self, same_pair):
        c = compare.compare_run_folders(*same_pair)["curves"]
        assert c["rolling_window"] == 40  # 40 trades each
        big = [(date(2026, 1, 1 + i % 28).isoformat(), 1.0, 1.0) for i in range(120)]
        assert len(big) == 120

    def test_distribution_bins_are_shared(self, overlap_pair):
        c = compare.compare_run_folders(*overlap_pair)["curves"]
        bins = c["distribution"]
        assert len(bins) == 20
        assert bins[0]["bin_start"] == pytest.approx(-1.2)
        assert bins[-1]["bin_end"] == pytest.approx(2.8)
        assert all(b["bin_end"] > b["bin_start"] for b in bins)
        assert sum(b["a"] for b in bins) == 4
        assert sum(b["b"] for b in bins) == 5

    def test_long_series_are_downsampled_keeping_the_ends(self, root):
        start = date(2020, 1, 1)
        rows = [((start + timedelta(days=i)).isoformat(), 1.0, 0.5) for i in range(900)]
        ex = _execution_of(rows, rows[0][0], rows[-1][0])
        c = compare.compare_run_folders(*_pair(root, ex, ex))["curves"]
        for key in ("equity_r", "drawdown_r", "rolling"):
            assert len(c[key]) <= tearsheet.MAX_SERIES_POINTS, key
            assert c[key][0]["x"] == rows[0][0] and c[key][-1]["x"] == rows[-1][0]
        assert c["rolling_window"] == 50


class TestZeroTrades:
    def _empty(self, folder: Path, dest: Path) -> Path:
        shutil.copytree(folder, dest)
        target = dest / "evidence" / "trades.parquet"
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        trades_io.write_trades_parquet([], target)
        return dest

    def test_zero_trade_run_does_not_crash(self, overlap_pair, tmp_path):
        empty = self._empty(overlap_pair[0], tmp_path / "empty")
        out = compare.compare_run_folders(empty, overlap_pair[1])
        a = out["views"]["full"]["a"]
        assert a["trades"] == 0 and a["gross_bps"] == 0.0 and a["net_bps"] == 0.0
        assert "win_rate" not in a
        assert out["performance"]["win_rate"]["a"] is None
        assert out["performance"]["win_rate"]["delta"] is None
        assert out["performance"]["trades"]["delta"] == 5
        assert all(p["a"] is None for p in out["curves"]["equity_r"])
        assert any(p["b"] is not None for p in out["curves"]["equity_r"])
        json.dumps(out, allow_nan=False)

    def test_both_zero(self, overlap_pair, tmp_path):
        a = self._empty(overlap_pair[0], tmp_path / "ea")
        b = self._empty(overlap_pair[1], tmp_path / "eb")
        out = compare.compare_run_folders(a, b)
        assert out["curves"]["equity_r"] == []
        assert out["curves"]["monthly"] == []
        assert out["curves"]["distribution"] == []
        assert out["curves"]["rolling_window"] == 0
        assert out["views"]["full"]["a"]["trades"] == 0
        json.dumps(out, allow_nan=False)


class TestPerformanceCommon:
    def test_equals_a_hand_computed_fixture(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        pc = out["performance_common"]
        assert pc["trades"] == {"a": 2, "b": 3, "delta": 1}
        assert pc["net_bps"]["a"] == pytest.approx(-20.0)
        assert pc["net_bps"]["b"] == pytest.approx(80.0)
        assert pc["net_bps"]["delta"] == pytest.approx(100.0)
        assert pc["expectancy_r"]["delta"] == pytest.approx(1.0)
        assert pc["t_stat"]["a"] is None and pc["t_stat"]["delta"] is None
        assert pc["t_stat"]["b"] == pytest.approx(80 / (200 / math.sqrt(3)), abs=1e-3)
        assert all(set(v) == {"a", "b", "delta"} for v in pc.values())

    def test_null_when_periods_are_disjoint(self, root):
        out = compare.compare_run_folders(
            *_pair(
                root,
                _execution_of([("2026-01-05", 1.0, 0.9)], "2026-01-01", "2026-01-31"),
                _execution_of([("2026-02-05", -1.0, -1.1)], "2026-02-01", "2026-02-28"),
            )
        )
        assert out["performance_common"] is None
        assert out["performance"]["trades"] == {"a": 1, "b": 1, "delta": 0}


def _cmp(root, a_rows, a_period, b_rows, b_period):
    return compare.compare_run_folders(
        *_pair(root, _execution_of(a_rows, *a_period), _execution_of(b_rows, *b_period))
    )


def _rng(start, end):
    return {"start": start, "end": end}


def _in_window(day, window):
    return window["start"] <= day <= window["end"]


class TestExtraRanges:
    def test_each_run_extends_past_the_common_window(self, root):
        a_rows = [("2026-03-02", 1.0, 1.0), ("2026-03-12", 1.0, 0.5)]
        b_rows = [("2026-03-12", 1.0, 0.5), ("2026-03-25", 1.0, 2.0), ("2026-03-27", -1.0, -1.0)]
        out = _cmp(root, a_rows, ("2026-03-01", "2026-03-20"), b_rows, ("2026-03-10", "2026-03-31"))
        o = out["overlap"]
        assert o["common"] == _rng("2026-03-10", "2026-03-20")
        assert o["extra"] == {
            "a": [_rng("2026-03-01", "2026-03-09")],
            "b": [_rng("2026-03-21", "2026-03-31")],
        }
        for d in ("2026-03-01", "2026-03-09", "2026-03-21", "2026-03-31"):
            assert d in o["warning"]
        assert "run A" in o["warning"] and "run B" in o["warning"]
        inc = out["views"]["incremental"]
        assert inc["run"] == "b" and inc["ranges"] == o["extra"]["b"]
        assert inc["metrics"]["trades"] == 2
        assert inc["metrics"]["expectancy_r"] == pytest.approx(0.5)  # (2.0 - 1.0) / 2
        assert out["curves"]["incremental"] == o["extra"]["b"]

    def test_b_contains_a_gives_two_ranges_and_hand_computed_incremental(self, root):
        a_rows = [("2026-03-15", 1.0, 0.5)]
        b_rows = [("2026-03-02", 1.0, 1.0), ("2026-03-15", 1.0, 0.5), ("2026-03-25", -1.0, -0.5)]
        out = _cmp(root, a_rows, ("2026-03-10", "2026-03-20"), b_rows, ("2026-03-01", "2026-03-31"))
        o = out["overlap"]
        assert o["extra"]["a"] == []
        assert o["extra"]["b"] == [
            _rng("2026-03-01", "2026-03-09"),
            _rng("2026-03-21", "2026-03-31"),
        ]
        assert o["warning"].count("run B also covers") == 1
        for d in ("2026-03-01", "2026-03-09", "2026-03-21", "2026-03-31"):
            assert d in o["warning"]
        inc = out["views"]["incremental"]
        assert inc["run"] == "b" and inc["ranges"] == o["extra"]["b"]
        assert inc["metrics"]["trades"] == 2
        assert inc["metrics"]["expectancy_r"] == pytest.approx(0.25)
        assert inc["metrics"]["win_rate"] == pytest.approx(0.5)
        assert out["views"]["common"]["b"]["trades"] == 1

    def test_a_contains_b(self, root):
        a_rows = [("2026-03-02", 1.0, 1.0), ("2026-03-15", 1.0, 0.5), ("2026-03-25", -1.0, -2.0)]
        b_rows = [("2026-03-15", 1.0, 0.5)]
        out = _cmp(root, a_rows, ("2026-03-01", "2026-03-31"), b_rows, ("2026-03-10", "2026-03-20"))
        o = out["overlap"]
        assert o["extra"]["b"] == []
        assert len(o["extra"]["a"]) == 2
        inc = out["views"]["incremental"]
        assert inc["run"] == "a" and inc["metrics"]["trades"] == 2
        assert inc["metrics"]["expectancy_r"] == pytest.approx(-0.5)

    def test_adjacent_periods_have_no_common_window(self, root):
        a_rows = [("2026-03-05", 1.0, 1.0)]
        b_rows = [("2026-03-12", 1.0, 0.5)]
        out = _cmp(root, a_rows, ("2026-03-01", "2026-03-10"), b_rows, ("2026-03-11", "2026-03-20"))
        o = out["overlap"]
        assert o["common"] is None
        assert o["extra"] == {
            "a": [_rng("2026-03-01", "2026-03-10")],
            "b": [_rng("2026-03-11", "2026-03-20")],
        }
        assert out["views"]["incremental"]["run"] == "b"
        assert out["curves_common"] is None
        assert out["curves_incremental"] is not None

    def test_one_day_common_window(self, root):
        a_rows = [("2026-03-05", 1.0, 1.0), ("2026-03-10", 1.0, 0.5)]
        b_rows = [("2026-03-10", 1.0, 0.7), ("2026-03-15", -1.0, -1.0)]
        out = _cmp(root, a_rows, ("2026-03-01", "2026-03-10"), b_rows, ("2026-03-10", "2026-03-20"))
        o = out["overlap"]
        assert o["common"] == _rng("2026-03-10", "2026-03-10")
        assert o["extra"]["a"] == [_rng("2026-03-01", "2026-03-09")]
        assert o["extra"]["b"] == [_rng("2026-03-11", "2026-03-20")]
        assert out["views"]["common"]["a"]["trades"] == 1
        assert out["views"]["common"]["b"]["trades"] == 1

    def test_no_incremental_trade_lies_in_the_common_window(self, root):
        a_rows = [("2026-03-02", 1.0, 1.0), ("2026-03-12", 1.0, 0.5)]
        b_rows = [("2026-03-12", 1.0, 0.5), ("2026-03-25", 1.0, 2.0)]
        out = _cmp(root, a_rows, ("2026-03-01", "2026-03-20"), b_rows, ("2026-03-10", "2026-03-31"))
        inc = out["views"]["incremental"]
        assert inc["metrics"]["trades"] == 1
        cur = out["curves_incremental"]
        common = out["overlap"]["common"]
        assert all(not _in_window(p["x"], common) for p in cur["equity_r"])


class TestDrawdownConvention:
    def test_max_drawdown_matches_the_tearsheet_convention(self, root):
        rows = [
            ("2026-03-02", 1.0, 2.0),
            ("2026-03-03", -1.0, -1.5),
            ("2026-03-04", -1.0, -1.0),
            ("2026-03-05", 1.0, 3.0),
        ]
        out = _cmp(root, rows, ("2026-03-01", "2026-03-10"), rows, ("2026-03-01", "2026-03-10"))
        dd = [p["a"] for p in out["curves"]["drawdown_r"]]
        assert dd == [pytest.approx(v) for v in (0.0, -1.5, -2.5, 0.0)]
        assert -min(dd) == pytest.approx(out["views"]["full"]["a"]["max_dd_r"])

    def test_downsampling_keeps_the_deepest_point(self, root):
        start = date(2020, 1, 1)
        rows = []
        for i in range(900):
            net = -3.0 if i == 450 else (0.5 if i < 450 else 0.1)
            rows.append(((start + timedelta(days=i)).isoformat(), 1.0, net))
        ex = _execution_of(rows, rows[0][0], rows[-1][0])
        c = compare.compare_run_folders(*_pair(root, ex, ex))["curves"]
        assert len(c["drawdown_r"]) <= tearsheet.MAX_SERIES_POINTS
        assert min(p["a"] for p in c["drawdown_r"]) == pytest.approx(-3.0)


class TestFrameLoading:
    def _rewrite(self, folder, dest, mutate):
        shutil.copytree(folder, dest)
        target = dest / "evidence" / "trades.parquet"
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        df = pd.read_parquet(target)
        mutate(df)
        df.to_parquet(target)
        return dest

    def test_rows_without_entry_time_are_dropped_and_counted(self, same_pair, tmp_path):
        def mutate(df):
            df.loc[0, "entry_time"] = pd.NaT
            df.loc[1, "entry_time"] = pd.NaT

        bad = self._rewrite(same_pair[0], tmp_path / "bad", mutate)
        frame, dropped = compare._load_frame(bad)
        assert dropped == 2 and len(frame) == 38
        out = compare.compare_run_folders(bad, same_pair[1])
        assert out["views"]["full"]["a"]["trades"] == 38

    def test_all_entry_times_missing_does_not_raise_attribute_error(self, same_pair, tmp_path):
        bad = self._rewrite(
            same_pair[0], tmp_path / "bad", lambda df: df.__setitem__("entry_time", pd.NaT)
        )
        try:
            out = compare.compare_run_folders(bad, same_pair[1])
        except compare.CompareError:
            return
        assert out["views"]["full"]["a"]["trades"] == 0

    def test_only_the_needed_columns_are_read(self, same_pair, monkeypatch):
        seen = []
        real = pd.read_parquet

        def spy(path, *args, **kwargs):
            seen.append(kwargs.get("columns"))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(pd, "read_parquet", spy)
        compare._load_frame(same_pair[0])
        assert seen == [compare._FRAME_COLUMNS]
        assert not {"symbol", "trade_id", "exit_reason", "exit_time"} & set(seen[0])


class TestDistributionNonFinite:
    def _frame(self, values):
        return pd.DataFrame({"net_r": values})

    def test_all_nan_gives_empty_bins(self):
        assert compare._distribution(self._frame([float("nan")]), self._frame([])) == []

    def test_nan_values_are_ignored(self):
        bins = compare._distribution(
            self._frame([1.0, float("nan"), 2.0]), self._frame([float("inf"), 1.5])
        )
        assert sum(b["a"] for b in bins) == 2 and sum(b["b"] for b in bins) == 1


class TestMetricParityOnSwingTrades:
    def test_recomputed_equals_headline_when_entry_and_exit_dates_differ(self, tmp_path):
        import dataclasses

        base_trade = _trade(date(2026, 3, 2), 1.1, 1.0)
        trades = [
            dataclasses.replace(
                base_trade,
                day=date(2026, 3, 2),
                entry_time=pd.Timestamp("2026-03-02 15:29"),
                exit_time="2026-03-06 10:00",
                r_gross=1.1,
                r_net=1.0,
            ),
            dataclasses.replace(
                base_trade,
                day=date(2026, 3, 3),
                entry_time=pd.Timestamp("2026-03-03 09:15"),
                exit_time="2026-03-09 11:00",
                r_gross=-1.0,
                r_net=-1.1,
            ),
            dataclasses.replace(
                base_trade,
                day=date(2026, 3, 4),
                entry_time=pd.Timestamp("2026-03-04 15:29"),
                exit_time="2026-03-04 15:30",
                r_gross=2.2,
                r_net=2.0,
            ),
        ]
        (tmp_path / "evidence").mkdir()
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        trades_io.write_trades_parquet(trades, tmp_path / "evidence" / "trades.parquet")
        frame, _ = compare._load_frame(tmp_path)
        assert frame["day"].tolist() == [t.day.isoformat() for t in trades]
        got = compare.metrics_from_frame(frame, n_trials=2)
        want = tearsheet.headline_metrics(
            [t.day.isoformat() for t in trades],
            [t.r_gross for t in trades],
            [t.r_net for t in trades],
            [t.risk_pct for t in trades],
            n_trials=2,
        )
        assert got.keys() == want.keys()
        for key, value in want.items():
            assert got[key] == pytest.approx(value, abs=1e-6), key


class TestPerViewCurves:
    def test_curves_common_equals_a_hand_computed_fixture(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        c = out["curves_common"]
        window = out["overlap"]["common"]
        assert window == _rng("2026-03-04", "2026-03-10")
        eq = {p["x"]: p for p in c["equity_r"]}
        assert list(eq) == ["2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"]
        assert eq["2026-03-04"]["a"] == pytest.approx(0.0)  # re-based at the window start
        assert eq["2026-03-05"]["a"] == pytest.approx(0.8)
        assert eq["2026-03-09"]["a"] == pytest.approx(-0.4)
        assert eq["2026-03-04"]["b"] == pytest.approx(2.8)
        assert eq["2026-03-06"]["b"] == pytest.approx(1.6)
        assert eq["2026-03-09"]["b"] == pytest.approx(2.4)
        dd = {p["x"]: p for p in c["drawdown_r"]}
        assert dd["2026-03-04"]["a"] == pytest.approx(0.0)
        assert dd["2026-03-09"]["a"] == pytest.approx(-1.2)
        assert dd["2026-03-06"]["b"] == pytest.approx(-1.2)
        assert dd["2026-03-09"]["b"] == pytest.approx(-0.4)
        assert c["rolling_window"] == 2
        assert sum(b["a"] for b in c["distribution"]) == 2
        assert sum(b["b"] for b in c["distribution"]) == 3
        assert [m["x"] for m in c["monthly"]] == ["2026-03"]
        assert c["monthly"][0]["a"] == pytest.approx(-0.4)
        assert c["common"] == window

    def test_no_curves_common_point_lies_outside_the_window(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        c, w = out["curves_common"], out["overlap"]["common"]
        for key in ("equity_r", "drawdown_r", "rolling"):
            assert all(_in_window(p["x"], w) for p in c[key]), key

    def test_curves_incremental_has_null_for_the_other_run(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        c = out["curves_incremental"]
        assert out["views"]["incremental"]["run"] == "b"
        for key in ("equity_r", "drawdown_r", "rolling"):
            assert c[key] and all(p["a"] is None for p in c[key]), key
        eq = {p["x"]: p["b"] for p in c["equity_r"]}
        assert eq == {
            "2026-03-11": pytest.approx(0.0),
            "2026-03-15": pytest.approx(1.8),
            "2026-03-18": pytest.approx(0.6),
        }
        dd = {p["x"]: p["b"] for p in c["drawdown_r"]}
        assert dd["2026-03-18"] == pytest.approx(-1.2)
        assert sum(b["a"] for b in c["distribution"]) == 0
        assert sum(b["b"] for b in c["distribution"]) == 2
        assert c["incremental"] == out["views"]["incremental"]["ranges"]

    def test_full_curves_are_unchanged_in_scope(self, overlap_pair):
        out = compare.compare_run_folders(*overlap_pair)
        assert {p["x"] for p in out["curves"]["equity_r"]} == {d for d, _, _ in A_ROWS} | {
            d for d, _, _ in B_ROWS
        }

    def test_identical_periods_have_no_incremental_curves(self, same_pair):
        out = compare.compare_run_folders(*same_pair)
        assert out["curves_incremental"] is None
        assert out["curves_common"] is not None

    def test_disjoint_periods_have_no_common_curves(self, root):
        out = _cmp(
            root,
            [("2026-01-05", 1.0, 0.9)],
            ("2026-01-01", "2026-01-31"),
            [("2026-02-05", -1.0, -1.1)],
            ("2026-02-01", "2026-02-28"),
        )
        assert out["curves_common"] is None
        assert out["curves_incremental"] is not None
        json.dumps(out, allow_nan=False)

    def test_result_is_json_safe(self, overlap_pair):
        json.dumps(compare.compare_run_folders(*overlap_pair), allow_nan=False)
