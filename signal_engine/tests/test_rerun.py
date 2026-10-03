"""record_run --rerun: re-record a frozen run folder against the data available now."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.change_class import (
    DATA_RANGE_CHANGE,
    DATASET_CHANGE,
    RESEARCH_RERUN,
)
from signal_engine.tests import test_record_run as base
from signal_engine.tests import test_run_artifact_write as raw
from signal_engine.tests.test_record_run import SUPPORTED

root = base.root
_stub_provenance = base._stub_provenance

SAME_FP = lambda s, i, a: "d" * 64  # noqa: E731


def _exec(n=40):
    return rr.Execution(**{**base._execution(n).__dict__, "symbols": ["AAA", "BBB"]})


def _first(root_):
    path = raw._record(root_, execute=lambda key: _exec())
    return json.loads(path.read_text(encoding="utf-8"))["run_id"]


def _argv(root_, *extra):
    return ["--root", str(root_), "--run-date", "2026-10-03", "--no-regimes", *extra]


def _hooks(fp=SAME_FP, git=raw.CLEAN):
    return {
        "git_state_fn": raw._git(git),
        "dataset_fingerprint_fn": fp,
        "readiness_missing_fn": lambda: [],
    }


def _snapshot(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _rerun(root_, run_id, execute, *extra, fp=SAME_FP):
    return rr.main(
        _argv(root_, "--rerun", run_id, *extra),
        execute=execute,
        supported=SUPPORTED,
        **_hooks(fp),
    )


def _new_manifest(root_, old_id):
    runs = raw._runs(root_)
    news = sorted(p for p in runs.iterdir() if p.is_dir() and p.name != old_id)
    assert len(news) == 1
    return json.loads((news[0] / "manifest.json").read_text(encoding="utf-8")), news[0]


class TestRerun:
    def test_identical_data_is_a_research_rerun_under_the_same_version(self, root):
        old = _first(root)
        assert _rerun(root, old, lambda key: _exec()) == 0
        manifest, _ = _new_manifest(root, old)
        assert manifest["parent_run_id"] == old
        assert manifest["strategy"]["version"] == "v1"
        assert manifest["change_classes"] == [RESEARCH_RERUN]

    def test_flat_record_carries_parent_and_default_note(self, root):
        old = _first(root)
        _rerun(root, old, lambda key: _exec())
        flats = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in raw._runs(root).glob("*.json")
            if p.stem != old
        ]
        assert len(flats) == 1
        assert flats[0]["parent_run_id"] == old
        assert flats[0]["change_note"] == f"rerun of {old}"
        assert flats[0]["strategy_version"] == "v1"

    def test_explicit_note_wins(self, root):
        old = _first(root)
        _rerun(root, old, lambda key: _exec(), "--note", "more data")
        flat = next(p for p in raw._runs(root).glob("*.json") if p.stem != old)
        assert json.loads(flat.read_text(encoding="utf-8"))["change_note"] == "more data"

    def test_longer_period_is_a_data_range_change(self, root):
        old = _first(root)
        assert _rerun(root, old, lambda key: _exec(60)) == 0
        manifest, _ = _new_manifest(root, old)
        assert manifest["change_classes"] == [DATA_RANGE_CHANGE]

    def test_different_dataset_fingerprint_is_a_dataset_change(self, root):
        old = _first(root)
        assert _rerun(root, old, lambda key: _exec(), fp=lambda s, i, a: "e" * 64) == 0
        manifest, _ = _new_manifest(root, old)
        assert manifest["change_classes"] == [DATASET_CHANGE]

    def test_old_run_folder_and_flat_record_are_untouched(self, root):
        old = _first(root)
        folder = raw._runs(root) / old
        flat = raw._runs(root) / f"{old}.json"
        before, flat_before = _snapshot(folder), flat.read_bytes()
        _rerun(root, old, lambda key: _exec(60))
        assert _snapshot(folder) == before
        assert flat.read_bytes() == flat_before

    def test_rerun_uses_the_frozen_spec_not_the_ideas_current_one(self, root):
        import yaml

        from signal_engine.tests.intake_fixtures import make_spec

        old = _first(root)
        idea = raw._runs(root).parent
        edited = make_spec(parameters={"ema_fast": 5, "ema_slow": 9, "atr_len": 3})
        (idea / "spec.yaml").write_text(yaml.safe_dump(edited), encoding="utf-8")
        assert _rerun(root, old, lambda key: _exec()) == 0
        manifest, _ = _new_manifest(root, old)
        assert manifest["change_classes"] == [RESEARCH_RERUN]
        assert manifest["strategy"]["version"] == "v1"
        assert (idea / "versions.yaml").read_text().count("fingerprint") <= 1

    def test_end_is_forwarded_when_given_and_none_otherwise(self, root):
        old = _first(root)
        seen = []

        def execute(key, settings=None, end=None):
            seen.append(end)
            return _exec()

        _rerun(root, old, execute)
        _rerun(root, old, execute, "--end", "2026-02-10")
        assert seen == [None, "2026-02-10"]

    def test_execute_without_an_end_parameter_still_works(self, root):
        old = _first(root)
        assert _rerun(root, old, lambda key: _exec(), "--end", "2026-02-10") == 0

    def test_run_without_a_manifest_is_refused(self, root):
        old = _first(root)
        legacy = raw._runs(root) / "2026-01-01-orb-v9"
        legacy.mkdir()
        (legacy / "report.json").write_text("{}")
        called = []
        code = _rerun(root, "2026-01-01-orb-v9", lambda key: called.append(key) or _exec())
        assert code == 1
        assert called == []
        assert old  # the real run is unaffected
        assert sorted(p.name for p in raw._runs(root).iterdir() if p.is_dir()) == sorted(
            [old, "2026-01-01-orb-v9"]
        )

    def test_unknown_run_id_errors(self, root):
        _first(root)
        called = []
        code = _rerun(root, "no-such-run", lambda key: called.append(key) or _exec())
        assert code == 1
        assert called == []

    def test_dirty_tree_still_refuses_a_rerun(self, root):
        old = _first(root)
        dirty = {**raw.CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/x.py"]}
        code = rr.main(
            _argv(root, "--rerun", old),
            execute=lambda key: _exec(),
            supported=SUPPORTED,
            **_hooks(git=dirty),
        )
        assert code == 1
        assert sorted(p.name for p in raw._runs(root).iterdir() if p.is_dir()) == [old]

    def test_tampered_run_folder_is_refused(self, root):
        old = _first(root)
        spec_file = raw._runs(root) / old / "strategy" / "spec.yaml"
        spec_file.chmod(0o644)
        spec_file.write_text("style: swing\n")
        assert _rerun(root, old, lambda key: _exec()) == 1
        assert sorted(p.name for p in raw._runs(root).iterdir() if p.is_dir()) == [old]

    def test_rerun_is_exclusive_with_idea_and_all(self, root):
        for other in (["--idea", "IDEA-009"], ["--all"]):
            with pytest.raises(SystemExit) as exc:
                rr.main(
                    _argv(root, "--rerun", "x", *other),
                    execute=lambda key: _exec(),
                    supported=SUPPORTED,
                )
            assert exc.value.code == 2

    def test_end_requires_rerun(self, root):
        with pytest.raises(SystemExit) as exc:
            rr.main(
                _argv(root, "--idea", "IDEA-009", "--end", "2026-02-10"),
                execute=lambda key: _exec(),
                supported=SUPPORTED,
            )
        assert exc.value.code == 2


class TestEndForwarding:
    def test_record_idea_passes_end_only_to_an_execute_that_accepts_it(self, root):
        seen = {}

        def accepts(key, settings=None, end=None):
            seen["end"] = end
            return _exec()

        raw._record(root, execute=accepts, end="2026-02-01")
        assert seen["end"] == "2026-02-01"
        raw._record(root, execute=lambda key, settings=None: _exec(), end="2026-02-01")


class TestExecuteBacktestEnd:
    def test_end_reaches_every_loader_call(self, monkeypatch):
        import pandas as pd

        from signal_engine.backtest import data

        calls = []

        def loader(**kw):
            calls.append(kw.get("end"))
            idx = pd.date_range("2026-01-05 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
            return {"AAA": pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)}

        monkeypatch.setattr(data, "from_historify", lambda **kw: loader(**kw))
        monkeypatch.setattr(data, "from_historify_daily", lambda **kw: loader(**kw))

        import signal_engine.backtest.harness as harness

        class FakeBT:
            def __init__(self, *a, **k):
                self.days = [pd.Timestamp("2026-01-05")]

            def trades(self, *a, **k):
                return [SimpleNamespace(symbol="AAA", entry=100.0, risk=1.0, position_id=None)]

        monkeypatch.setattr(harness, "Backtest", FakeBT)
        monkeypatch.setattr(rr, "sample_trades", lambda trades, n: list(trades))
        monkeypatch.setattr(rr, "random_entry_baseline_intraday", lambda *a, **k: {})
        rr.execute_backtest("orb", end="2026-01-09")
        assert len(calls) == 2  # the universe load and the baseline reload
        assert all(c is not None and str(c).startswith("2026-01-09") for c in calls)
        calls.clear()
        rr.execute_backtest("orb")
        assert calls == [None, None]
