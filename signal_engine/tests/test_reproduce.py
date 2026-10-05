"""reproduce: rerun a recorded run from its folder alone and compare it row for row."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest

from signal_engine.backtest import manifest as mf
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import reproduce as rp
from signal_engine.tests import test_manifest as tm
from signal_engine.tests import test_record_run as base
from signal_engine.tests import test_run_artifact_write as raw
from signal_engine.tests.published_fixtures import fake_resolution
from signal_engine.tests.test_record_run import SUPPORTED
from signal_engine.timeutils import IST

root = base.root
_stub_provenance = base._stub_provenance

# The autouse stub replaces manifest.dataset_fingerprint, so keep the real one from import time.
REAL_FINGERPRINT = mf.dataset_fingerprint
SAME_FP = lambda s, i, a: "d" * 64  # noqa: E731


def _exec(n=40, mutate=None, symbols=("AAA", "BBB")):
    execution = base._execution(n)
    trades = list(execution.trades)
    if mutate is not None:
        trades = mutate(trades)
    return rr.Execution(**{**execution.__dict__, "trades": trades, "symbols": list(symbols)})


def _record(root_, **kw):
    kw.setdefault("execute", lambda key: _exec())
    path = raw._record(root_, **kw)
    return raw._folder(path)


def _snapshot(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _reproduce(folder, root_, execute=None, **kw):
    kw.setdefault("git_state_fn", raw._git(raw.CLEAN))
    kw.setdefault("dataset_fingerprint_fn", SAME_FP)
    kw.setdefault("now", raw.NOW)
    return rp.reproduce(
        folder,
        root=root_,
        execute=execute or (lambda key, settings=None, end=None: _exec()),
        supported=SUPPORTED,
        **kw,
    )


def _change_one(trades):
    out = list(trades)
    out[5] = replace(out[5], r_net=out[5].r_net + 0.5)
    return out


class TestReproduced:
    def test_identical_reexecution_passes(self, root):
        folder = _record(root)
        report = _reproduce(folder, root)
        assert report.reproducible is True
        assert report.reasons == []
        assert all(report.checks.values()) and report.checks
        assert report.compared_trades == 40
        assert report.run_id == folder.name
        assert report.attempted_ist == raw.NOW.astimezone(IST).isoformat()

    def test_rerun_is_given_the_frozen_settings_and_data_as_of(self, root):
        folder = _record(root)
        seen = {}

        def execute(key, settings=None, end=None):
            seen.update(key=key, settings=settings, end=end)
            return _exec()

        _reproduce(folder, root, execute)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert seen["key"] == "orb"
        assert seen["end"] == manifest["dataset"]["data_as_of"]
        assert seen["settings"]["cost_bps"] == 16.0
        assert seen["settings"]["mis_squareoff_min"] == 900

    def test_execute_without_end_or_settings_is_called_with_the_key_only(self, root):
        folder = _record(root)
        calls = []

        def execute(key):
            calls.append(key)
            return _exec()

        assert _reproduce(folder, root, execute).reproducible is True
        assert calls == ["orb"]

    def test_report_is_written_outside_the_run_folder(self, root):
        folder = _record(root)
        before = _snapshot(folder)
        flat = folder.parent / f"{folder.name}.json"
        flat_before = flat.read_bytes()
        _reproduce(folder, root)
        target = folder.parent / f"{folder.name}.reproductions"
        files = sorted(target.glob("*.json"))
        assert [f.name for f in files] == ["20261002T110000-ist.json"]
        written = json.loads(files[0].read_text(encoding="utf-8"))
        assert written["reproducible"] is True
        assert written["run_id"] == folder.name
        assert set(written["checks"]) == set(rp.CHECKS)
        assert _snapshot(folder) == before
        assert flat.read_bytes() == flat_before
        assert mf.verify_artifacts(folder) == []

    def test_write_report_false_writes_nothing(self, root):
        folder = _record(root)
        _reproduce(folder, root, write_report=False)
        assert not (folder.parent / f"{folder.name}.reproductions").exists()

    def test_two_attempts_in_the_same_second_both_survive(self, root):
        folder = _record(root)
        _reproduce(folder, root)
        _reproduce(folder, root)
        target = folder.parent / f"{folder.name}.reproductions"
        assert len(list(target.glob("*.json"))) == 2


class TestNotReproduced:
    def test_one_changed_trade_names_the_row_and_column(self, root):
        folder = _record(root)
        report = _reproduce(
            folder, root, lambda key, settings=None, end=None: _exec(mutate=_change_one)
        )
        assert report.reproducible is False
        assert report.checks["trades_match"] is False
        assert any("row 5" in r and "net_r" in r for r in report.reasons)

    def test_trade_count_difference_is_reported(self, root):
        folder = _record(root)
        report = _reproduce(
            folder, root, lambda key, settings=None, end=None: _exec(mutate=lambda t: t[:-3])
        )
        assert report.reproducible is False
        assert any("37" in r and "40" in r for r in report.reasons)

    def test_float_noise_below_tolerance_passes(self, root):
        folder = _record(root)

        def nudge(trades):
            out = list(trades)
            out[2] = replace(out[2], r_net=out[2].r_net + 1e-12)
            return out

        report = _reproduce(folder, root, lambda key, settings=None, end=None: _exec(mutate=nudge))
        assert report.reproducible is True

    def test_different_code_commit_fails_with_a_commit_reason(self, root):
        folder = _record(root)
        other = {**raw.CLEAN, "code_commit": "b" * 40}
        report = _reproduce(folder, root, git_state_fn=raw._git(other))
        assert report.reproducible is False
        assert report.checks["code_unchanged"] is False
        assert any(r.startswith("code commit differs:") for r in report.reasons)

    def test_dirty_tree_fails(self, root):
        folder = _record(root)
        dirty = {**raw.CLEAN, "dirty_paths": ["signal_engine/backtest/engine.py"]}
        report = _reproduce(folder, root, git_state_fn=raw._git(dirty))
        assert report.reproducible is False
        assert any(r.startswith("dirty working tree") for r in report.reasons)

    def test_doc_only_dirt_is_ignored(self, root):
        folder = _record(root)
        dirty = {**raw.CLEAN, "dirty_paths": ["signal_engine/docs/x.md"]}
        assert _reproduce(folder, root, git_state_fn=raw._git(dirty)).reproducible is True

    def test_dataset_fingerprint_change_fails_with_a_fingerprint_reason(self, root, monkeypatch):
        folder = _record(root)
        _fingerprint_now(monkeypatch, "e" * 64)
        report = _reproduce(folder, root)
        assert report.reproducible is False
        assert report.checks["dataset_unchanged"] is False
        assert any(
            "dataset fingerprint changed: history was corrected or backfilled" in r
            for r in report.reasons
        )

    def test_non_reproducible_manifest_is_reported_with_its_reason(self, root):
        dirty = {**raw.CLEAN, "code_dirty": True, "dirty_paths": ["signal_engine/x.py"]}
        folder = _record(root, git=dirty, allow_dirty=True)
        calls = []

        def execute(key, settings=None, end=None):
            calls.append(key)
            return _exec()

        report = _reproduce(folder, root, execute, git_state_fn=raw._git(dirty))
        assert report.reproducible is False
        assert report.checks["manifest_reproducible"] is False
        assert any("not reproducible" in r and "code_dirty" in r for r in report.reasons)
        assert calls == []  # nothing is rerun once the manifest already says it cannot match

    def test_run_without_symbols_is_not_reproducible(self, root):
        folder = _record(root, execute=lambda key: _exec(symbols=()))
        report = _reproduce(folder, root)
        assert report.reproducible is False
        assert any("dataset_fingerprint" in r for r in report.reasons)

    def test_tampered_artifact_fails_the_first_check_and_does_not_raise(self, root):
        folder = _record(root)
        target = folder / "evidence" / "trades.parquet"
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"x")
        report = _reproduce(folder, root)
        assert report.reproducible is False
        assert report.checks["artifacts_verified"] is False
        assert any("hash mismatch" in r for r in report.reasons)

    def test_unreadable_manifest_does_not_raise(self, root):
        folder = _record(root)
        folder.chmod(0o755)
        (folder / "manifest.json").chmod(0o644)
        (folder / "manifest.json").write_text("not json")
        report = _reproduce(folder, root)
        assert report.reproducible is False
        assert report.reasons

    def test_failing_execute_is_a_failed_check_not_an_exception(self, root):
        folder = _record(root)

        def boom(key, settings=None, end=None):
            raise RuntimeError("store offline")

        report = _reproduce(folder, root, boom)
        assert report.reproducible is False
        assert any("rerun failed" in r and "store offline" in r for r in report.reasons)

    def test_original_run_is_unchanged_after_a_failed_attempt(self, root):
        folder = _record(root)
        before = _snapshot(folder)
        _reproduce(folder, root, lambda key, settings=None, end=None: _exec(mutate=_change_one))
        assert _snapshot(folder) == before


def _fingerprint_now(monkeypatch, value):
    """Make the recorded version's fingerprint over the same symbols and as-of read `value` now."""
    res = fake_resolution()
    monkeypatch.setattr(res, "run_fingerprint", lambda symbols, end: value)
    monkeypatch.setattr(rp, "_resolution_for", lambda manifest: res)


class TestPublishedDatasetFingerprint:
    """Replaced the live-store fingerprint check (plan Phase 4, owner decision 2): a schema 4 run
    is compared on the published version's `run_fingerprint`."""

    def test_unchanged_history_passes(self, root, monkeypatch):
        folder = _record(root)
        _fingerprint_now(monkeypatch, "d" * 64)
        report = _reproduce(folder, root)
        assert report.reproducible is True
        assert report.checks["dataset_unchanged"] is True

    def test_altered_historical_close_fails(self, root, monkeypatch):
        folder = _record(root)
        _fingerprint_now(monkeypatch, "f" * 64)
        report = _reproduce(folder, root)
        assert report.reproducible is False
        assert report.checks["dataset_unchanged"] is False
        assert any("dataset fingerprint changed" in r for r in report.reasons)

    def test_fingerprint_is_taken_over_the_recorded_symbols_and_as_of(self, root, monkeypatch):
        folder = _record(root)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        seen = []
        res = fake_resolution()
        monkeypatch.setattr(res, "run_fingerprint",
                            lambda symbols, end: seen.append((list(symbols), end)) or "d" * 64)
        monkeypatch.setattr(rp, "_resolution_for", lambda m: res)
        assert _reproduce(folder, root).reproducible is True
        assert seen == [(["AAA", "BBB"], manifest["dataset"]["data_as_of"])]


class TestCli:
    def _argv(self, root_, run_id):
        return ["--root", str(root_), "--reproduce", run_id]

    def _hooks(self):
        return {
            "git_state_fn": raw._git(raw.CLEAN),
            "dataset_fingerprint_fn": SAME_FP,
            "readiness_missing_fn": lambda: [],
        }

    def test_exit_zero_when_reproducible(self, root, capsys):
        folder = _record(root)
        code = rr.main(
            self._argv(root, folder.name),
            execute=lambda key, settings=None, end=None: _exec(),
            supported=SUPPORTED,
            **self._hooks(),
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "reproducible" in out and folder.name in out

    def test_exit_one_when_not_reproducible(self, root, capsys):
        folder = _record(root)
        code = rr.main(
            self._argv(root, folder.name),
            execute=lambda key, settings=None, end=None: _exec(mutate=_change_one),
            supported=SUPPORTED,
            **self._hooks(),
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "not reproducible" in out and "net_r" in out

    def test_unknown_run_exits_one(self, root):
        _record(root)
        code = rr.main(
            self._argv(root, "no-such-run"),
            execute=lambda key, settings=None, end=None: _exec(),
            supported=SUPPORTED,
            **self._hooks(),
        )
        assert code == 1

    def test_reproduce_is_exclusive_with_idea(self, root):
        with pytest.raises(SystemExit) as exc:
            rr.main(["--root", str(root), "--reproduce", "x", "--idea", "IDEA-009"])
        assert exc.value.code == 2


class TestNoVacuousPass:
    def test_zero_recorded_trades_is_not_reproducible(self, root, monkeypatch):
        import pandas as pd

        folder = _record(root)
        monkeypatch.setattr(rp, "read_trades_parquet", lambda path: pd.DataFrame())
        monkeypatch.setattr(rp, "trades_to_frame", lambda trades: pd.DataFrame())
        attempt = rp._Attempt()
        rp._compare_trades(attempt, folder, _exec(mutate=lambda t: []))
        assert attempt.checks["trades_match"] is False
        assert any("zero trades" in r for r in attempt.reasons)

    def test_no_symbols_fails_the_dataset_check(self):
        attempt = rp._Attempt()
        manifest = {
            "run_id": "r",
            "dataset": {"symbol_count": 0, "interval": "5m", "data_as_of": "d", "fingerprint": "x"},
        }
        rp._check_dataset(attempt, manifest, _exec(symbols=()), SAME_FP)
        assert attempt.checks["dataset_unchanged"] is False
        assert any("no symbols" in r for r in attempt.reasons)

    def test_naive_now_is_rejected(self, root):
        from datetime import datetime

        folder = _record(root)
        with pytest.raises(ValueError, match="now must be timezone-aware"):
            _reproduce(folder, root, now=datetime(2026, 10, 2, 11, 0))
        assert not (folder.parent / f"{folder.name}.reproductions").exists()
