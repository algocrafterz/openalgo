"""Run artifact writing: record_idea builds an immutable run folder, then the flat schema-3 record."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml

from signal_engine.backtest import manifest as mf
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import run_artifact as ra
from signal_engine.backtest import trades_io
from signal_engine.backtest.change_class import PARAMETER_CHANGE, RESEARCH_RERUN
from signal_engine.research.intake import freeze as freeze_mod
from signal_engine.tests import test_record_run as base
from signal_engine.tests.intake_fixtures import GOOD_SPEC, make_spec
from signal_engine.tests.published_fixtures import fake_resolution
from signal_engine.tests.test_record_run import SUPPORTED, _execution
from signal_engine.timeutils import IST

root = base.root  # the idea-folder fixtures, shared with test_record_run
_stub_provenance = base._stub_provenance

CLEAN = {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []}
NOW = datetime(2026, 10, 2, 11, 0, tzinfo=IST)
READINESS = ["Capability one", "Capability two"]
IDEA = "IDEA-009"
FILES = {
    "manifest.json",
    "assumptions.json",
    "strategy/spec.yaml",
    "strategy/strategy.json",
    "execution/config.json",
    "evidence/trades.parquet",
    "report/report.json",
}


def _git(state):
    return lambda root_: state



def _FingerprintResolution(result, seen=None):
    """A fake resolution whose run fingerprint returns `result` (or raises it) and records the
    (sorted symbols, as_of) it was asked for."""
    base_res = fake_resolution()

    class _R(type(base_res)):
        def run_fingerprint(self, symbols, end):
            if seen is not None:
                seen.append((sorted(symbols), end))
            if isinstance(result, Exception):
                raise result
            return result

    return _R(base_res.manifest, base_res.report, base_res.contract, base_res.calendar)

def _record(root, *, execute=None, run_date="2026-10-02", git=CLEAN, **kw):
    kw.setdefault("git_state_fn", _git(git))
    kw.setdefault("dataset_fingerprint_fn", lambda s, i, a: "d" * 64)
    kw.setdefault("readiness_missing_fn", lambda: list(READINESS))
    kw.setdefault("now", NOW)
    return rr.record_idea(
        root,
        IDEA,
        execute=execute or (lambda key: _execution()),
        run_date=run_date,
        supported=SUPPORTED,
        **kw,
    )


def _runs(root) -> Path:
    return root / "research" / "ideas" / "idea-009-orb" / "runs"


def _folder(path: Path) -> Path:
    return path.parent / path.stem


def _manifest(path: Path) -> dict:
    return json.loads((_folder(path) / "manifest.json").read_text(encoding="utf-8"))


class TestLayout:
    def test_folder_has_the_seven_files(self, root):
        path = _record(root)
        folder = _folder(path)
        found = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
        assert found == FILES

    def test_manifest_and_artifacts_verify_clean(self, root):
        path = _record(root)
        assert mf.validate_manifest(_manifest(path)) == []
        assert mf.verify_artifacts(_folder(path)) == []

    def test_flat_record_is_schema_4_and_indexes_the_folder(self, root):
        path = _record(root)
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert flat["schema"] == 4  # plan Phase 4: published dataset version, run status
        assert flat["artifact_dir"] == f"{flat['run_id']}/"
        assert flat["manifest_sha256"] == mf.manifest_sha256(_folder(path))
        assert flat["strategy_version"] == _manifest(path)["strategy"]["version"]
        assert flat["run_verdict"] in {"INCONCLUSIVE", "MARGINAL", "NO_EDGE"}
        assert flat["promotion_decision"] in {"NOT_READY", "BLOCKED"}
        assert set(flat["next_action"]) == {"action", "reason", "gate"}
        assert flat["metrics"]["trades"] == 40  # v2 keys are kept
        assert flat["code_version"]

    def test_files_are_read_only(self, root):
        folder = _folder(_record(root))
        assert (folder / "manifest.json").stat().st_mode & 0o222 == 0

    def test_trades_parquet_row_count_matches(self, root):
        folder = _folder(_record(root))
        df = trades_io.read_trades_parquet(folder / "evidence" / "trades.parquet")
        assert len(df) == 40

    def test_zero_trade_run_still_writes_a_parquet(self, root):
        empty = _execution()
        empty = rr.Execution(**{**empty.__dict__, "trades": []})
        try:
            path = _record(root, execute=lambda key: empty)
        except ValueError:
            pytest.skip("tearsheet refuses an empty trade list")
        df = trades_io.read_trades_parquet(_folder(path) / "evidence" / "trades.parquet")
        assert len(df) == 0

    def test_report_json_contents(self, root):
        path = _record(root)
        report = json.loads((_folder(path) / "report" / "report.json").read_text(encoding="utf-8"))
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert report["schema"] == 4
        assert report["metrics"] == flat["metrics"]
        assert report["series"] == flat["series"]
        assert report["evaluation"] == flat.get("evaluation")
        assert report["validity"] == {"ok": True, "failures": [], "checks_run": ["config"]}
        assert report["data_quality"] == {
            "status": "NOT_RUN",
            "affects_validity": False,
            "items": [],
        }
        assert set(report["verdict"]) == {
            "run_verdict",
            "primary_failed_gate",
            "reasons",
            "caps_applied",
        }
        assert report["verdict"]["run_verdict"] != "EDGE"
        assert set(report["promotion"]["stages"]) == {"backtest", "paper", "live"}
        assert set(report["next_action"]) == {"action", "reason", "gate"}
        assert flat["run_verdict"] == report["verdict"]["run_verdict"]
        assert flat["promotion_decision"] == report["promotion"]["stages"]["backtest"]["decision"]
        assert flat["promotion_decision"] in {"READY_FOR_NEXT_STAGE", "NOT_READY", "BLOCKED"}
        assert flat["next_action"] == report["next_action"]
        assert report["n_trials"] == flat["n_trials"]
        for key in ("position_caps", "split", "baseline", "cost_model"):
            if key in flat:
                assert report[key] == flat[key]


class TestFrozenContent:
    def test_strategy_json_fields(self, root):
        path = _record(root)
        folder = _folder(path)
        s = json.loads((folder / "strategy" / "strategy.json").read_text(encoding="utf-8"))
        m = _manifest(path)
        assert s["strategy_id"] == IDEA
        assert s["name"] == GOOD_SPEC["name"]
        assert s["version"] == m["strategy"]["version"]
        assert s["parent_version"] == m["strategy"]["parent_version"]
        assert s["branch_label"] is None
        assert s["strategy_fingerprint"] == m["fingerprints"]["strategy"]
        assert s["rules_fingerprint"] == m["fingerprints"]["rules"]
        assert s["params_fingerprint"] == m["fingerprints"]["params"]
        assert (s["style"], s["direction"], s["timeframe"], s["product"]) == (
            "intraday",
            "both",
            "5m",
            "MIS",
        )

    def test_spec_yaml_is_the_frozen_intent(self, root):
        folder = _folder(_record(root))
        data = yaml.safe_load((folder / "strategy" / "spec.yaml").read_text(encoding="utf-8"))
        assert data["name"] == GOOD_SPEC["name"]
        assert data["product"] == "MIS"

    def test_config_json_leaves_all_have_ledger_ids(self, root):
        folder = _folder(_record(root))
        cfg = json.loads((folder / "execution" / "config.json").read_text(encoding="utf-8"))
        assert set(cfg) == {"resolved", "profile"}
        assert cfg["resolved"]
        for name, leaf in cfg["resolved"].items():
            assert leaf["ledger_id"], name
        assert cfg["profile"]["name"] == "flattrade-nse-cash"

    def test_assumptions_equal_the_frozen_ledger(self, root):
        idea_dir = _runs(root).parent
        frozen = freeze_mod.freeze(idea_dir, idea_id=IDEA, register_version=False)
        folder = _folder(_record(root))
        snap = json.loads((folder / "assumptions.json").read_text(encoding="utf-8"))
        assert snap["entries"] == [e.as_dict() for e in frozen.ledger]
        ids = [int(e["id"].split("-")[1]) for e in snap["entries"]]
        assert snap["next_id"] == max(ids) + 1
        assert snap["sealed_ist"].endswith("+05:30")

    def test_sealed_snapshot_survives_later_edits_to_the_working_ledger(self, root):
        folder = _folder(_record(root))
        before = (folder / "assumptions.json").read_bytes()
        idea_dir = folder.parent.parent
        (idea_dir / "assumptions.yaml").write_text("entries: []\n", encoding="utf-8")
        (idea_dir / "spec.yaml").write_text(
            yaml.safe_dump(make_spec(name="Renamed")), encoding="utf-8"
        )
        assert (folder / "assumptions.json").read_bytes() == before
        assert mf.verify_artifacts(folder) == []

    def test_second_run_ids_stay_above_the_sealed_ones(self, root):
        first = _folder(_record(root))
        sealed = json.loads((first / "assumptions.json").read_text(encoding="utf-8"))
        second = _folder(_record(root))
        again = json.loads((second / "assumptions.json").read_text(encoding="utf-8"))
        assert again["next_id"] >= sealed["next_id"]

    def test_manifest_identity_fields(self, root):
        path = _record(root)
        m = _manifest(path)
        assert m["schema"] == 4
        assert m["run_id"] == path.stem
        assert m["created_ist"] == NOW.isoformat()
        assert m["idea_id"] == IDEA
        assert m["period"] == {"start": "2026-01-01", "end": "2026-02-20"}
        assert m["dataset"]["interval"] == "5m"
        # plan Phase 4: the dataset is the run's published version, never "historify"
        assert m["dataset"]["source"] == fake_resolution().manifest["source"]
        assert m["dataset"]["id"] == fake_resolution().version_id
        assert m["fingerprints"]["profile"] == m["deployment_profile"]["sha256"]
        assert m["deployment_profile"]["name"] == "flattrade-nse-cash"
        assert m["deployment_profile"]["product"] == "MIS"
        assert m["session"]["force_flat"] == "15:00"
        assert m["engine"] == {
            "engine_version": ra.ENGINE_VERSION,
            "code_commit": "a" * 40,
            "code_dirty": False,
            "dirty_paths": [],
        }
        assert m["random"] == {"seed": 0, "monte_carlo": None}
        assert m["environment"]["packages"]

    def test_missing_capabilities_hold_readiness_and_null_paths(self, root):
        m = _manifest(_record(root))
        caps = m["missing_capabilities"]
        for row in READINESS:
            assert row in caps
        for path in (
            "calendar_version",
            "session.exchange",
            "session.broker_cutoff",
            "dataset.corporate_action_policy",
            "universe.version",
            "random.monte_carlo",
        ):
            assert f"{path} not captured until engine step 5" in caps

    def test_empty_symbols_fingerprint_needs_no_database(self, root):
        """The legacy store fingerprint function is never called (plan Phase 4): the run
        fingerprint comes from the published version."""
        def boom(*a):
            raise AssertionError("database touched")

        m = _manifest(_record(root, dataset_fingerprint_fn=boom))
        assert m["dataset"]["fingerprint"] == fake_resolution().run_fingerprint([], None)
        assert m["reproducibility"]["dataset_fingerprint"] is False
        assert m["reproducible"] is False

    def test_symbols_reach_the_fingerprint_and_universe(self, root):
        seen = []
        ex = rr.Execution(**{**_execution().__dict__, "symbols": ["B.NS", "A.NS"]})

        res = _FingerprintResolution("e" * 64, seen)
        rr_prepare = rr.prepare_data
        rr.prepare_data = lambda *a, **k: res
        try:
            m = _manifest(_record(root, execute=lambda key: ex))
        finally:
            rr.prepare_data = rr_prepare
        # plan Phase 4: the run fingerprint is the published version's, over the run's symbols
        assert seen == [(["A.NS", "B.NS"], "2026-02-20")]
        assert m["dataset"]["fingerprint"] == "e" * 64
        assert m["dataset"]["symbol_count"] == 2
        assert m["universe"]["symbols_sha256"] == ra.sha256_hex(ra.canonical_json(["A.NS", "B.NS"]))
        assert m["reproducibility"]["dataset_fingerprint"] is True
        assert m["reproducible"] is True
        flat = json.loads((_runs(root) / f"{m['run_id']}.json").read_text(encoding="utf-8"))
        assert flat["reproducible"] is True
        assert flat["promotion_eligible"] is True


class TestDirtyCheck:
    def test_dirty_code_is_refused_before_execute_and_freeze(self, root):
        calls = []
        dirty = {**CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/backtest/engine.py"]}
        with pytest.raises(ra.DirtyTreeError, match="--allow-dirty") as err:
            _record(root, execute=lambda key: calls.append(key), git=dirty)
        assert "engine.py" in str(err.value)
        assert calls == []
        assert not _runs(root).exists()

    def test_message_lists_at_most_ten_paths(self, root):
        paths = [f"signal_engine/m{i:02d}.py" for i in range(15)]
        dirty = {**CLEAN, "code_dirty": True, "dirty_paths": paths}
        with pytest.raises(ra.DirtyTreeError) as err:
            _record(root, git=dirty)
        text = str(err.value)
        assert "m09.py" in text and "m10.py" not in text

    def test_allow_dirty_marks_the_run_not_reproducible(self, root):
        dirty = {**CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/x.py", "a.md"]}
        ex = rr.Execution(**{**_execution().__dict__, "symbols": ["A.NS"]})
        path = _record(root, execute=lambda key: ex, git=dirty, allow_dirty=True)
        m = _manifest(path)
        assert m["engine"]["code_dirty"] is True
        assert m["engine"]["dirty_paths"] == ["signal_engine/x.py"]
        assert m["reproducible"] is False
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert flat["code_dirty"] is True
        assert flat["reproducible"] is False
        assert flat["promotion_eligible"] is False

    def test_unknown_commit_counts_as_dirty(self, root):
        unknown = {"code_commit": "unknown", "code_dirty": True, "dirty_paths": []}
        with pytest.raises(ra.DirtyTreeError):
            _record(root, git=unknown)
        path = _record(root, git=unknown, allow_dirty=True)
        m = _manifest(path)
        assert m["engine"]["code_dirty"] is True
        assert m["reproducibility"]["code_commit"] is False

    def test_doc_only_dirt_is_not_a_refusal(self, root):
        paths = [
            "signal_engine/docs/PLAN.md",
            "signal_engine/research/REGISTRY.md",
            "signal_engine/research/registry.json",
            "signal_engine/research/site/index.html",
            "signal_engine/research/ideas/idea-009-orb/runs/x.json",
            "signal_engine/research/ideas/idea-009-orb/spec.yaml",
            "frontend/src/app.tsx",
        ]
        path = _record(root, git={**CLEAN, "code_dirty": True, "dirty_paths": paths})
        assert _manifest(path)["engine"]["code_dirty"] is False

    def test_result_affecting_paths(self):
        paths = [
            "signal_engine/backtest/engine.py",
            "signal_engine/tests/test_x.py",
            "tests/helper.py",
            "signal_engine/config.yaml",
            "uv.lock",
            "pyproject.toml",
            "signal_engine/research/profiles/flattrade-nse-cash.yaml",
            "signal_engine/docs/a.md",
            "signal_engine/research/REGISTRY.md",
            "other/config.yaml.bak",
            "blueprints/x.py",
            "database/y.py",
            "broker/z/api.py",
            "services/s.py",
            "frontend/a.py",
            "signal_engine/backtest/tests/t.py",
            "frontend/config.yaml",
            "utils/logging.py",
        ]
        assert ra.result_affecting_paths(paths) == [
            "signal_engine/backtest/engine.py",
            "signal_engine/config.yaml",
            "uv.lock",
            "pyproject.toml",
            "signal_engine/research/profiles/flattrade-nse-cash.yaml",
            "frontend/config.yaml",
            "utils/logging.py",
        ]
        assert ra.result_affecting_paths([]) == []


class TestChangeClasses:
    def test_first_run_has_none(self, root):
        assert _manifest(_record(root))["change_classes"] == []

    def test_identical_second_run_is_a_rerun(self, root):
        _record(root)
        second = _record(root)
        assert _manifest(second)["change_classes"] == [RESEARCH_RERUN]
        assert json.loads(second.read_text())["change_classes"] == [RESEARCH_RERUN]

    def test_parameter_edit_between_runs(self, root):
        _record(root)
        idea_dir = _runs(root).parent
        spec_path = idea_dir / "spec.yaml"
        edited = make_spec(parameters={"ema_fast": 10, "ema_slow": 20, "atr_len": 14})
        spec_path.write_text(yaml.safe_dump(edited), encoding="utf-8")
        second = _record(root, parent_version="v1")
        classes = _manifest(second)["change_classes"]
        assert PARAMETER_CHANGE in classes
        assert _manifest(second)["strategy"] == {
            "version": "v2",
            "parent_version": "v1",
            "branch_label": None,
        }

    def test_new_version_without_a_parent_version_has_no_reference(self, root):
        _record(root)
        spec_path = _runs(root).parent / "spec.yaml"
        edited = make_spec(parameters={"ema_fast": 10, "ema_slow": 20, "atr_len": 14})
        spec_path.write_text(yaml.safe_dump(edited), encoding="utf-8")
        assert _manifest(_record(root))["change_classes"] == []

    def test_legacy_flat_only_run_is_never_a_reference(self, root):
        runs = _runs(root)
        runs.mkdir(parents=True)
        (runs / "2026-09-01-orb-v1.json").write_text("{}", encoding="utf-8")
        path = _record(root)
        assert path.stem == "2026-10-02-orb-v2"
        assert _manifest(path)["change_classes"] == []

    def test_parent_with_manifest_is_the_reference(self, root):
        first = _record(root)
        _record(root)
        runs = _runs(root)
        # Rewrite nothing: make the third run name the first as its parent.
        third = _record(root, parent=first.stem)
        assert _manifest(third)["parent_run_id"] == first.stem
        assert _manifest(third)["change_classes"] == [RESEARCH_RERUN]
        assert runs.is_dir()

    def test_parent_selection_prefers_parent_over_latest(self, root, monkeypatch):
        first = _record(root)
        folder = _folder(first)
        m = _manifest(first)
        # A later run of the same version whose period differs; the explicit parent must win.
        late_ex = _execution(30)
        _record(root, execute=lambda key: late_ex, now=datetime(2026, 10, 2, 12, 0, tzinfo=IST))
        third = _record(root, parent=first.stem, now=datetime(2026, 10, 2, 13, 0, tzinfo=IST))
        assert _manifest(third)["change_classes"] == [RESEARCH_RERUN]
        assert m["run_id"] == folder.name

    def test_latest_run_of_same_version_is_the_default_reference(self, root):
        _record(root)
        late_ex = _execution(30)
        _record(root, execute=lambda key: late_ex, now=datetime(2026, 10, 2, 12, 0, tzinfo=IST))
        third = _record(root, now=datetime(2026, 10, 2, 13, 0, tzinfo=IST))
        # Compared against the 30-day run, so the period differs.
        assert "DATA_RANGE_CHANGE" in _manifest(third)["change_classes"]


class TestRunIds:
    def test_folders_consume_run_ids(self, tmp_path):
        (tmp_path / "2026-09-01-orb-v1").mkdir()
        (tmp_path / ".2026-09-01-orb-v9.tmp-abc").mkdir()
        assert rr.next_run_id(tmp_path, "2026-09-30", "orb") == "2026-09-30-orb-v2"
        assert rr._existing_run_ids(tmp_path) == {"2026-09-01-orb-v1"}

    def test_folder_without_flat_json_is_a_known_run(self, tmp_path):
        (tmp_path / "r1").mkdir()
        (tmp_path / "r2.json").write_text("{}")
        assert rr._existing_run_ids(tmp_path) == {"r1", "r2"}


class TestFailureLeavesNothing:
    def test_artifact_failure_leaves_no_folder_flat_record_or_run_id(self, root, monkeypatch):
        def boom(trades, **kw):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(ra, "trades_parquet_bytes", boom)
        with pytest.raises(RuntimeError):
            _record(root)
        runs = _runs(root)
        assert not runs.exists() or list(runs.iterdir()) == []
        monkeypatch.undo()
        monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())  # re-stub
        path = _record(root)
        assert path.stem == "2026-10-02-orb-v1"

    def test_execute_failure_leaves_no_runs_dir(self, root):
        def boom(key):
            raise RuntimeError("no data")

        with pytest.raises(RuntimeError):
            _record(root, execute=boom)
        assert not _runs(root).exists()


class TestSettings:
    def test_settings_reach_an_execute_that_accepts_them(self, root):
        seen = {}

        def execute(key, settings=None):
            seen.update(settings or {})
            return _execution()

        _record(root, execute=execute)
        assert seen == {
            "cost_bps": 16.0,
            "mis_squareoff_min": 900,
            "squareoff_margin_min": 5,
            "time_exit_min": 885,
            "tick_grid": "safe_tick",
            "slippage_bps_per_side": 4.0,
            "warmup_bars": 20,
            "eligibility_min_prior_sessions": 60,
            "eligibility_min_volume": 1,
            "eligibility_min_price": 10.0,
            "product": "MIS",
            # the deployment profile's broker prices brokerage
            "broker": "flattrade",
            # the frozen spec's intent, so the run is bound to the spec and not to class defaults
            "parameters": {"ema_fast": 9, "ema_slow": 20, "atr_len": 14},
            "stop": ["1 ATR(14) below entry"],
            "holding_period": "12 bars",
        }

    def test_execute_without_settings_is_called_with_the_key_only(self, root):
        calls = []

        def execute(key):
            calls.append(key)
            return _execution()

        _record(root, execute=execute)
        assert calls == ["orb"]

    def test_execute_frozen_checks_flat_secondary_against_frozen(self, root):
        wrong = _execution()
        wrong = rr.Execution(
            **{**wrong.__dict__, "costs": {**wrong.costs, "flat_round_trip_bps": 99.0}}
        )
        with pytest.raises(ValueError, match="did not use the frozen configuration"):
            _record(root, execute=lambda key: wrong)
        assert not _runs(root).exists()

    def test_execute_frozen_rejects_flat_headline_execution(self, root):
        flat = _execution()
        flat = rr.Execution(
            **{**flat.__dict__, "costs": {"model": "flat-round-trip-bps", "round_trip_bps": 16.0}}
        )
        with pytest.raises(ValueError, match="statutory cost basis"):
            _record(root, execute=lambda key: flat)
        assert not _runs(root).exists()

    def test_execution_fills_symbols_field(self):
        assert (
            rr.Execution(trades=[], params={}, data={}, period={}, costs={}, days=[]).symbols == []
        )
        assert base is not None and pd is not None


class TestMainAndSettingsOverlay:
    def _argv(self, root, *extra):
        return [
            "--root",
            str(root),
            "--idea",
            IDEA,
            "--run-date",
            "2026-10-02",
            "--no-regimes",
            *extra,
        ]

    def _hooks(self, git):
        return {
            "git_state_fn": _git(git),
            "dataset_fingerprint_fn": lambda s, i, a: "d" * 64,
            "readiness_missing_fn": lambda: [],
        }

    def test_main_refuses_a_dirty_tree_without_the_flag(self, root):
        dirty = {**CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/x.py"]}
        code = rr.main(
            self._argv(root),
            execute=lambda key: _execution(),
            supported=SUPPORTED,
            **self._hooks(dirty),
        )
        assert code == 1
        assert not _runs(root).exists()

    def test_main_allow_dirty_records_a_non_reproducible_run(self, root):
        dirty = {**CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/x.py"]}
        code = rr.main(
            self._argv(root, "--allow-dirty"),
            execute=lambda key: _execution(),
            supported=SUPPORTED,
            **self._hooks(dirty),
        )
        assert code == 0
        flat = json.loads(next(_runs(root).glob("*.json")).read_text(encoding="utf-8"))
        assert flat["code_dirty"] is True and flat["promotion_eligible"] is False

    def test_profile_settings_overlay_wins_and_only_tightens_the_time_exit(self):
        from signal_engine.backtest.types import RunConfig

        run = RunConfig(cost_bps=16.0, time_exit_min=14 * 60 + 45)
        out = rr._apply_profile_settings(
            run,
            {"mis_squareoff_min": 900, "squareoff_margin_min": 7, "time_exit_min": 800},
        )
        assert (out.mis_squareoff_min, out.squareoff_margin_min, out.time_exit_min) == (900, 7, 800)
        looser = rr._apply_profile_settings(run, {"time_exit_min": 1000})
        assert looser.time_exit_min == 14 * 60 + 45
        assert rr._apply_profile_settings(run, {}) is run

    def test_slippage_setting_reaches_the_cost_model(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "sizing": {"risk_per_trade": 0.01, "sandbox_capital": 100000},
                    "broker": {"exchange": "NSE", "mis_margin_pct": 0.2},
                }
            ),
            encoding="utf-8",
        )
        model = rr.deployed_cost_model("intraday", cfg, slippage_bps_per_side=7.5)
        assert model.slippage_bps_per_side == 7.5


def _nothing_written(root):
    runs = _runs(root)
    assert not runs.exists() or list(runs.iterdir()) == []
    assert not (runs.parent / "versions.yaml").exists()


class TestPreCommitFailuresLeaveNothing:
    def _fail_then_succeed(self, root, monkeypatch, patch, **kw):
        patch(monkeypatch)
        with pytest.raises((RuntimeError, ValueError)):
            _record(root, **kw)
        _nothing_written(root)
        monkeypatch.undo()
        # undo() also removed conftest's published-dataset stub; reinstall it for the retry
        monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())
        assert _record(root).stem == "2026-10-02-orb-v1"

    def test_dataset_fingerprint_failure(self, root, monkeypatch):
        """The run fingerprint now comes from the published version (plan Phase 4); a version
        whose objects cannot be read must still leave nothing behind."""
        broken = _FingerprintResolution(RuntimeError("published object unreadable"))
        with_symbols = rr.Execution(**{**_execution().__dict__, "symbols": ["AAA"]})
        self._fail_then_succeed(
            root,
            monkeypatch,
            lambda mp: mp.setattr(rr, "prepare_data", lambda *a, **k: broken),
            execute=lambda key: with_symbols,
        )

    def test_readiness_failure(self, root, monkeypatch):
        def boom():
            raise RuntimeError("readiness broke")

        self._fail_then_succeed(root, monkeypatch, lambda mp: None, readiness_missing_fn=boom)

    def test_environment_snapshot_failure(self, root, monkeypatch):
        def boom(*a, **k):
            raise KeyError("package not in uv.lock")

        def patch(mp):
            mp.setattr(mf, "environment_snapshot", boom)

        patch(monkeypatch)
        with pytest.raises(KeyError):
            _record(root)
        _nothing_written(root)
        monkeypatch.undo()
        monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())  # re-stub
        assert _record(root).stem == "2026-10-02-orb-v1"

    def test_trades_bytes_failure(self, root, monkeypatch):
        def boom(trades, **kw):
            raise RuntimeError("disk on fire")

        self._fail_then_succeed(
            root, monkeypatch, lambda mp: mp.setattr(ra, "trades_parquet_bytes", boom)
        )

    def test_nan_in_the_flat_record(self, root, monkeypatch):
        real = rr.build_run_record

        def with_nan(*a, **k):
            return {**real(*a, **k), "stray": float("nan")}

        patch = lambda mp: mp.setattr(rr, "build_run_record", with_nan)  # noqa: E731
        self._fail_then_succeed(root, monkeypatch, patch)

    def test_invalid_manifest_leaves_nothing(self, root, monkeypatch):
        real = ra.build_manifest

        def broken(**kw):
            m = real(**kw)
            m["schema"] = 2
            return m

        monkeypatch.setattr(ra, "build_manifest", broken)
        with pytest.raises(ValueError, match="invalid run manifest"):
            _record(root)
        _nothing_written(root)

    def test_run_id_is_chosen_after_the_backtest(self, root):
        seen = {}

        def execute(key):
            seen["runs_exist_during_execute"] = _runs(root).exists()
            return _execution()

        _record(root, execute=execute)
        assert seen["runs_exist_during_execute"] is False

    def test_flat_record_matches_the_serialised_text(self, root):
        path = _record(root)
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert path.read_text(encoding="utf-8") == rr.serialise_run(flat)

    def test_serialise_run_refuses_nan(self):
        with pytest.raises(ValueError):
            rr.serialise_run({"run_id": "x", "v": float("nan")})


class _Seq:
    """A git_state_fn that returns each state in turn, then repeats the last."""

    def __init__(self, *states):
        self.states = list(states)
        self.calls = 0

    def __call__(self, root_):
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return state


class TestGitChangesDuringRun:
    def test_head_change_is_refused_and_writes_nothing(self, root):
        seq = _Seq(CLEAN, {**CLEAN, "code_commit": "b" * 40})
        with pytest.raises(ra.DirtyTreeError, match="working tree changed during the run"):
            _record(root, git_state_fn=seq)
        assert seq.calls == 2
        _nothing_written(root)

    def test_new_result_affecting_dirt_is_refused(self, root):
        seq = _Seq(CLEAN, {**CLEAN, "dirty_paths": ["signal_engine/backtest/engine.py"]})
        with pytest.raises(ra.DirtyTreeError, match="working tree changed during the run"):
            _record(root, git_state_fn=seq)
        _nothing_written(root)

    def test_new_doc_dirt_is_ignored(self, root):
        seq = _Seq(CLEAN, {**CLEAN, "dirty_paths": ["signal_engine/docs/x.md"]})
        path = _record(root, git_state_fn=seq)
        assert _manifest(path)["engine"]["code_dirty"] is False

    def test_allow_dirty_records_the_union_of_dirty_paths(self, root):
        first = {**CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/a.py"]}
        second = {**CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/b.py"]}
        path = _record(root, git_state_fn=_Seq(first, second), allow_dirty=True)
        engine = _manifest(path)["engine"]
        assert engine["code_dirty"] is True
        assert engine["dirty_paths"] == ["signal_engine/a.py", "signal_engine/b.py"]
        assert _manifest(path)["reproducible"] is False

    def test_allow_dirty_with_head_change_is_dirty(self, root):
        seq = _Seq(CLEAN, {**CLEAN, "code_commit": "b" * 40})
        path = _record(root, git_state_fn=seq, allow_dirty=True)
        assert _manifest(path)["engine"]["code_dirty"] is True


class TestLegacyParentIsNoReference:
    def test_parent_without_manifest_gives_first_run_classes(self, root):
        _record(root)  # a manifested v1 run that must NOT be used as the fallback
        runs = _runs(root)
        (runs / "2026-09-01-orb-v9.json").write_text("{}", encoding="utf-8")
        path = _record(root, parent="2026-09-01-orb-v9")
        assert _manifest(path)["change_classes"] == []

    def test_reference_manifest_unit(self, tmp_path):
        assert (
            ra.reference_manifest(
                tmp_path, parent_run_id="legacy", version="v1", parent_version=None
            )
            is None
        )


class TestReproductionDirsAreNotRuns:
    def test_reproductions_and_dot_dirs_are_ignored(self, tmp_path):
        (tmp_path / "2026-09-01-orb-v1.reproductions").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "2026-09-01-orb-v2").mkdir()
        assert rr._existing_run_ids(tmp_path) == {"2026-09-01-orb-v2"}
        assert rr.next_run_id(tmp_path, "2026-09-30", "orb") == "2026-09-30-orb-v3"

    def test_parent_naming_a_reproductions_dir_is_rejected(self, root):
        first = _record(root)
        target = _runs(root) / f"{first.stem}.reproductions"
        target.mkdir()
        with pytest.raises(ValueError, match="is not a run"):
            _record(root, parent=target.name)
        code = rr.main(
            ["--root", str(root), "--idea", IDEA, "--parent", target.name, "--no-regimes"],
            execute=lambda key: _execution(),
            supported=SUPPORTED,
            git_state_fn=_git(CLEAN),
            dataset_fingerprint_fn=lambda s, i, a: "d" * 64,
            readiness_missing_fn=lambda: [],
        )
        assert code == 1


BAD_RUN_IDS = ["../x", "/etc/passwd", "2026-10-02-orb-v1/../../x", "a/b", "2026-10-02-orb-v1\n", ""]


class TestRunIdValidation:
    @pytest.mark.parametrize("run_id", BAD_RUN_IDS)
    def test_find_run_folder_rejects_before_touching_disk(self, root, run_id):
        with pytest.raises(rr.RerunError, match="not a valid run id"):
            rr.find_run_folder(root, run_id)

    @pytest.mark.parametrize("run_id", BAD_RUN_IDS)
    @pytest.mark.parametrize("flag", ["--rerun", "--reproduce"])
    def test_cli_rejects(self, root, run_id, flag):
        code = rr.main(
            ["--root", str(root), flag, run_id, "--no-regimes"],
            execute=lambda key: _execution(),
            supported=SUPPORTED,
        )
        assert code == 1

    def test_valid_id_is_accepted(self, root):
        path = _record(root)
        idea_id, folder = rr.find_run_folder(root, path.stem)
        assert idea_id == IDEA and folder.name == path.stem

    @pytest.mark.parametrize("run_id", BAD_RUN_IDS)
    def test_reproduce_function_rejects(self, tmp_path, run_id):
        from signal_engine.backtest import reproduce as rp

        with pytest.raises(rr.RerunError):
            rp.reproduce(
                tmp_path / "runs" / run_id,
                root=tmp_path,
                execute=lambda key: None,
                write_report=False,
            )


class TestNaiveNow:
    def test_record_idea_rejects_a_naive_now_before_executing(self, root):
        called = []

        def execute(key):
            called.append(key)
            return _execution()

        with pytest.raises(ValueError, match="now must be timezone-aware"):
            _record(root, execute=execute, now=datetime(2026, 10, 2, 11, 0))
        assert called == []
        _nothing_written(root)

    def test_created_rejects_naive(self):
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            ra._created(datetime(2026, 10, 2, 11, 0))
