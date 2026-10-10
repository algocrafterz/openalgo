"""Step 6c: validity checks, the truncation check and the trial ledger wired into the recorded run.

Covers the record flow end to end (record_idea with stub executions and the CLI), the G0.2 and
G2.5 evaluators, the registry check on trials.yaml and the cap-lift fixture (every gate the
engine can evaluate passes, so only the readiness table can hold the verdict at marginal).
"""

import dataclasses
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
import yaml

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import promotion as promo
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import verdict as verdict_mod
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.truncation import TruncationResult, truncation_check
from signal_engine.backtest.types import EntrySignal
from signal_engine.research import build_registry as br
from signal_engine.research.intake import trials as trials_mod
from signal_engine.tests import test_record_run as base
from signal_engine.tests.intake_fixtures import make_spec
from signal_engine.tests.test_backtest_ledgers import SwingScript
from signal_engine.tests.test_data_session_integration import (  # noqa: F401
    EPOCH0,
    SETTINGS,
    store,
    store_db,
)
from signal_engine.tests.test_evaluation import _book, _days, _evaluate, _row, _trade
from signal_engine.tests.test_research_registry import VALID, _write_idea
from signal_engine.tests.test_research_registry import base as registry_base  # noqa: F401
from signal_engine.tests.test_run_artifact_write import (
    NOW,
    READINESS,
    SUPPORTED,
    _execution,
    _folder,
    _git,
    _record,
    _runs,
)

root = base.root
_stub_provenance = base._stub_provenance

IDEA_DIR = Path("research/ideas/idea-009-orb")
OK_TRUNCATION = {
    "ok": True,
    "checked": [{"symbol": "A", "cut": "2026-01-10"}],
    "mismatches": [],
    "skipped": [],
}
BAD_TRUNCATION = {
    "ok": False,
    "checked": [{"symbol": "A", "cut": "2026-01-10"}],
    "mismatches": [
        {
            "symbol": "A",
            "cut": "2026-01-10",
            "kind": "trade_differs",
            "detail": "trade entered 2026-01-03 09:20 differs on entry",
        }
    ],
    "skipped": [],
}


def _exe(**over):
    return rr.Execution(**{**_execution().__dict__, **over})


def _report(path: Path) -> dict:
    return json.loads((_folder(path) / "report" / "report.json").read_text(encoding="utf-8"))


def _gate_row(report: dict, gate: str) -> dict:
    return _row(report["evaluation"], gate)


def _trials_path(root_: Path) -> Path:
    return root_ / IDEA_DIR / "trials.yaml"


def _edit_params(root_: Path, **parameters) -> None:
    spec_path = root_ / IDEA_DIR / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(make_spec(parameters=parameters)), encoding="utf-8")


# ---- validity wired into the run ---------------------------------------------------------


class TestValidityInReport:
    def test_report_validity_lists_the_checks_that_ran(self, root):
        report = _report(_record(root))
        assert report["validity"] == {"ok": True, "failures": [], "checks_run": ["config"]}

    def test_data_quality_failure_makes_the_run_invalid_data(self, root):
        """Plan D17 (Phase 4): a data-quality failure is the run status INVALID_DATA with no
        verdict (it used to be an INCONCLUSIVE verdict)."""
        dq = {"status": "WARNING", "affects_validity": True, "items": ["12 impossible bars"]}
        report = _report(_record(root, execute=lambda key: _exe(data_quality=dq)))
        assert report["validity"]["ok"] is False
        assert report["validity"]["checks_run"] == ["data_quality", "config"]
        failure = report["validity"]["failures"][0]
        assert failure["check"] == "data_quality" and "12 impossible bars" in failure["detail"]
        assert report["verdict"] is None and report["run_status"] == "INVALID_DATA"
        assert "12 impossible bars" in report["next_action"]
        assert report["promotion"]["stages"]["backtest"]["decision"] == "NOT_READY"

    def test_passing_data_quality_is_recorded_as_run(self, root):
        dq = {"status": "PASS", "affects_validity": False, "items": []}
        report = _report(_record(root, execute=lambda key: _exe(data_quality=dq)))
        assert report["validity"]["ok"] is True
        assert "data_quality" in report["validity"]["checks_run"]

    def test_lineage_defect_is_named(self, root):
        # ledgers that hold none of the trades' ids: every trade reference is dangling
        ledgers = LedgerRecorder().to_frames()
        report = _report(_record(root, execute=lambda key: _exe(ledgers=ledgers)))
        checks = {f["check"] for f in report["validity"]["failures"]}
        assert checks == {"lineage"}
        assert "lineage" in report["validity"]["checks_run"]
        assert report["verdict"]["run_verdict"] == "INCONCLUSIVE"

    def test_flat_record_verdict_agrees_with_report(self, root):
        path = _record(root, execute=lambda key: _exe(truncation=BAD_TRUNCATION))
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert flat["run_verdict"] == "INCONCLUSIVE"
        assert br._validate_run(flat, _runs(root)) == []


class TestTruncationInReport:
    def test_ok_result_passes_validity_and_g02(self, root):
        report = _report(_record(root, execute=lambda key: _exe(truncation=OK_TRUNCATION)))
        assert report["validity"]["ok"] is True
        assert "truncation" in report["validity"]["checks_run"]
        assert _gate_row(report, "G0.2")["status"] == "pass"

    def test_mismatch_fails_validity_and_g02(self, root):
        report = _report(_record(root, execute=lambda key: _exe(truncation=BAD_TRUNCATION)))
        assert report["validity"]["ok"] is False
        failure = [f for f in report["validity"]["failures"] if f["check"] == "truncation"]
        assert failure and "trade_differs" in failure[0]["detail"]
        row = _gate_row(report, "G0.2")
        assert row["status"] == "fail" and "trade_differs" in row["evidence"]
        assert report["verdict"]["run_verdict"] == "INCONCLUSIVE"

    def test_absent_truncation_is_missing_not_passed(self, root):
        report = _report(_record(root))
        assert "truncation" not in report["validity"]["checks_run"]
        assert _gate_row(report, "G0.2")["status"] == "not_evaluated"


class TestTruncationFlag:
    def _stub(self, seen):
        def execute(key, run_truncation=True):
            seen.append(run_truncation)
            return _exe(truncation=OK_TRUNCATION if run_truncation else None)

        return execute

    def test_record_idea_runs_truncation_by_default(self, root):
        seen = []
        _record(root, execute=self._stub(seen))
        assert seen == [True]

    def test_record_idea_can_skip_it(self, root):
        seen = []
        report = _report(_record(root, execute=self._stub(seen), run_truncation=False))
        assert seen == [False]
        assert "truncation" not in report["validity"]["checks_run"]

    def test_cli_flag_skips_it(self, root, capsys):
        seen = []
        code = rr.main(
            [
                "--root",
                str(root),
                "--idea",
                "IDEA-009",
                "--no-truncation",
                "--run-date",
                "2026-10-02",
            ],
            execute=self._stub(seen),
            supported=SUPPORTED,
            git_state_fn=_git({"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []}),
            dataset_fingerprint_fn=lambda s, i, a: "d" * 64,
            readiness_missing_fn=lambda: list(READINESS),
        )
        assert code == 0 and seen == [False]

    def test_cli_default_runs_it(self, root):
        seen = []
        code = rr.main(
            ["--root", str(root), "--idea", "IDEA-009", "--run-date", "2026-10-02"],
            execute=self._stub(seen),
            supported=SUPPORTED,
            git_state_fn=_git({"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []}),
            dataset_fingerprint_fn=lambda s, i, a: "d" * 64,
            readiness_missing_fn=lambda: list(READINESS),
        )
        assert code == 0 and seen == [True]


class TestExecuteBacktestTruncation:
    def test_real_run_carries_a_truncation_result(self, store):  # noqa: F811
        exe = rr.execute_backtest("k", settings=SETTINGS, dataset=store["res"])
        assert exe.truncation is not None
        assert exe.truncation["ok"] is True, exe.truncation["mismatches"]
        assert exe.truncation["checked"]

    def test_skipped_leaves_none(self, store):  # noqa: F811
        exe = rr.execute_backtest("k", settings=SETTINGS, run_truncation=False,
                                  dataset=store["res"])
        assert exe.truncation is None

    def test_at_most_three_evenly_spaced_symbols_and_the_engine_used(
        self,
        store,  # noqa: F811
        monkeypatch,
    ):
        calls = []

        def spy(strategy, params, frames, run, *, engine=None, **kw):
            calls.append((sorted(frames), engine, run))
            return TruncationResult(True, [{"symbol": "x", "cut": "c"}])

        monkeypatch.setattr(rr, "truncation_check", spy)
        exe = rr.execute_backtest("k", settings=SETTINGS, dataset=store["res"])
        symbols, engine, run = calls[0]
        assert 1 <= len(symbols) <= 3 and symbols == sorted(symbols)
        assert engine is None or engine.__name__ == "simulate"
        assert run.cost_bps > 0
        got = exe.truncation
        assert got["ok"] is True and got["checked"] == [{"symbol": "x", "cut": "c"}]
        assert got["mismatches"] == [] and got["skipped"] == []


# ---- trial ledger ------------------------------------------------------------------------


class TestTrialLedger:
    def test_first_run_is_one_trial(self, root):
        path = _record(root)
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert flat["n_trials"] == 1
        assert [t.kind for t in trials_mod.load_trials(root / IDEA_DIR)] == ["material"]

    def test_parameter_change_adds_a_trial(self, root):
        _record(root)
        _edit_params(root, ema_fast=10, ema_slow=20, atr_len=14)
        second = _record(root, parent_version="v1")
        assert json.loads(second.read_text(encoding="utf-8"))["n_trials"] == 2
        assert trials_mod.count(root / IDEA_DIR).total == 2

    def test_identical_rerun_adds_nothing(self, root):
        _record(root)
        _edit_params(root, ema_fast=10, ema_slow=20, atr_len=14)
        _record(root, parent_version="v1")
        again = _record(root)
        assert json.loads(again.read_text(encoding="utf-8"))["n_trials"] == 2
        assert trials_mod.count(root / IDEA_DIR).total == 2

    def test_dataset_only_change_adds_nothing(self, root):
        _record(root)
        second = _record(root, dataset_fingerprint_fn=lambda s, i, a: "e" * 64)
        assert json.loads(second.read_text(encoding="utf-8"))["n_trials"] == 1
        assert trials_mod.count(root / IDEA_DIR).total == 1

    def test_external_trials_are_added_with_a_reason(self, root):
        path = _record(root, external_trials=3, external_reason="grid search in a notebook")
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert flat["n_trials"] == 4
        assert trials_mod.count(root / IDEA_DIR) == trials_mod.TrialCount(1, 3, 4)
        again = _record(root)
        assert json.loads(again.read_text(encoding="utf-8"))["n_trials"] == 4

    def test_external_trials_need_a_reason_before_anything_runs(self, root):
        ran = []

        def execute(key):
            ran.append(key)
            return _execution()

        with pytest.raises(ValueError, match="reason"):
            _record(root, execute=execute, external_trials=2)
        with pytest.raises(ValueError, match="reason"):
            _record(root, execute=execute, external_trials=2, external_reason="  ")
        assert ran == []
        assert not _trials_path(root).exists()
        assert not _runs(root).exists()

    def test_the_old_trials_argument_is_gone(self, root):
        with pytest.raises(TypeError):
            _record(root, trials=5)

    def test_failed_execution_leaves_the_ledger_untouched(self, root):
        def boom(key):
            raise RuntimeError("backtest failed")

        with pytest.raises(RuntimeError):
            _record(root, execute=boom, external_trials=2, external_reason="x")
        assert not _trials_path(root).exists()

    def test_artifact_failure_after_execute_still_counts(self, root):
        # a trial is an EVALUATED change: the run executed, only its artifacts failed to write
        _record(root)
        _edit_params(root, ema_fast=10, ema_slow=20, atr_len=14)

        def broken():
            raise RuntimeError("readiness table unreadable")

        with pytest.raises(RuntimeError):
            _record(
                root,
                parent_version="v1",
                readiness_missing_fn=broken,
                external_trials=2,
                external_reason="notebook sweep",
            )
        assert trials_mod.count(root / IDEA_DIR) == trials_mod.TrialCount(2, 2, 4)
        assert len(list(_runs(root).glob("*.json"))) == 1  # no second run was committed

    def test_retry_after_an_artifact_failure_does_not_double_count(self, root):
        _edit_params(root, ema_fast=10, ema_slow=20, atr_len=14)

        def broken():
            raise RuntimeError("readiness table unreadable")

        with pytest.raises(RuntimeError):
            _record(root, readiness_missing_fn=broken)
        retry = _record(root)
        assert json.loads(retry.read_text(encoding="utf-8"))["n_trials"] == 1
        assert trials_mod.count(root / IDEA_DIR).total == 1

    def test_ledger_is_written_before_the_artifacts(self, root):
        seen = {}
        real = rr.run_artifact.prepare_run_artifact

        def spy(**kw):
            seen["trials"] = trials_mod.count(root / IDEA_DIR).total
            return real(**kw)

        import unittest.mock as mock

        with mock.patch.object(rr.run_artifact, "prepare_run_artifact", spy):
            _record(root)
        assert seen["trials"] == 1

    def test_a_failing_ledger_write_is_logged_and_raised(self, root, monkeypatch):
        logged = []

        class Spy:
            def exception(self, *a, **k):
                logged.append(a)

            def __getattr__(self, name):
                return lambda *a, **k: None

        def boom(*a, **k):
            raise trials_mod.TrialError("disk full")

        monkeypatch.setattr(rr, "logger", Spy())
        monkeypatch.setattr(rr.trials_mod, "register_material", boom)
        with pytest.raises(trials_mod.TrialError):
            _record(root)
        assert logged
        assert not _runs(root).exists()

    def test_universe_refresh_is_not_a_trial(self, root):
        _record(root, execute=lambda key: _exe(symbols=["A", "B"]))
        second = _record(root, execute=lambda key: _exe(symbols=["A", "B", "C"]))
        assert json.loads(second.read_text(encoding="utf-8"))["n_trials"] == 1
        assert trials_mod.count(root / IDEA_DIR).total == 1

    def test_the_run_counts_its_own_trial_prospectively(self, root):
        # the first run's evaluation already uses n_trials 1 although the ledger is written later
        report = _report(_record(root))
        assert report["trials"] == {
            "material": 1,
            "external": 0,
            "total": 1,
            "source": "trials.yaml",
        }
        assert report["n_trials"] == 1

    def test_report_and_flat_record_carry_the_trials_block(self, root):
        path = _record(root, external_trials=2, external_reason="notebook sweep")
        expected = {"material": 1, "external": 2, "total": 3, "source": "trials.yaml"}
        flat = json.loads(path.read_text(encoding="utf-8"))
        assert flat["trials"] == expected and flat["n_trials"] == 3
        assert _report(path)["trials"] == expected
        assert br._validate_run(flat, _runs(root)) == []

    def test_notes_name_the_source_of_the_count(self, root):
        flat = json.loads(_record(root).read_text(encoding="utf-8"))
        assert "trials.yaml" in flat["notes"]
        assert "defaulted" not in flat["notes"] and "lower bound" not in flat["notes"]

    def test_hurdle_uses_the_ledger_number(self, root):
        one = json.loads(_record(root).read_text(encoding="utf-8"))
        many = json.loads(
            _record(root, external_trials=40, external_reason="sweep").read_text(encoding="utf-8")
        )
        assert many["metrics"]["hurdle_t"] > one["metrics"]["hurdle_t"]
        g22 = _row(many["evaluation"], "G2.2")
        assert "41 trials" in g22["threshold"]


class TestTrialsCli:
    def _main(self, root_, *args):
        return rr.main(
            ["--root", str(root_), "--idea", "IDEA-009", "--run-date", "2026-10-02", *args],
            execute=lambda key: _execution(),
            supported=SUPPORTED,
            git_state_fn=_git({"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []}),
            dataset_fingerprint_fn=lambda s, i, a: "d" * 64,
            readiness_missing_fn=lambda: list(READINESS),
        )

    def test_old_trials_flag_is_rejected_with_the_replacement(self, root, capsys):
        with pytest.raises(SystemExit) as exc:
            self._main(root, "--trials", "4")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--external-trials" in err and "--reason" in err
        assert "Traceback" not in err
        assert not _trials_path(root).exists()

    def test_external_trials_with_reason(self, root):
        assert self._main(root, "--external-trials", "5", "--reason", "notebook sweep") == 0
        assert trials_mod.count(root / IDEA_DIR).total == 6

    def test_external_trials_without_reason_is_a_usage_error(self, root, capsys):
        with pytest.raises(SystemExit) as exc:
            self._main(root, "--external-trials", "5")
        assert exc.value.code == 2
        assert "--reason" in capsys.readouterr().err

    def test_reason_without_external_trials_is_a_usage_error(self, root):
        with pytest.raises(SystemExit) as exc:
            self._main(root, "--reason", "why")
        assert exc.value.code == 2

    def test_external_trials_with_rerun_is_a_usage_error(self, root, capsys):
        first = self._main(root)
        assert first == 0
        with pytest.raises(SystemExit) as exc:
            rr.main(
                ["--root", str(root), "--rerun", "2026-10-02-orb-v1", "--external-trials", "2"]
                + ["--reason", "r"],
                execute=lambda key: _execution(),
                supported=SUPPORTED,
            )
        assert exc.value.code == 2
        assert "trials add" in capsys.readouterr().err
        assert trials_mod.count(root / IDEA_DIR).total == 1

    def test_external_trials_with_all_is_a_usage_error(self, root, capsys):
        with pytest.raises(SystemExit) as exc:
            self._main_all(root)
        assert exc.value.code == 2
        assert "trials add" in capsys.readouterr().err

    def _main_all(self, root_):
        return rr.main(
            ["--root", str(root_), "--all", "--external-trials", "2", "--reason", "r"],
            execute=lambda key: _execution(),
            supported=SUPPORTED,
        )

    def test_record_rerun_refuses_external_trials(self, root):
        _record(root)
        with pytest.raises(ValueError, match="trials add"):
            rr.record_rerun(
                root,
                "2026-10-02-orb-v1",
                execute=lambda key: _execution(),
                run_date="2026-10-03",
                supported=SUPPORTED,
                external_trials=2,
                external_reason="r",
            )
        assert trials_mod.count(root / IDEA_DIR).total == 1

    @pytest.mark.parametrize("bad", ["0", "-1", "x"])
    def test_external_trials_must_be_a_positive_integer(self, root, bad):
        with pytest.raises(SystemExit) as exc:
            self._main(root, "--external-trials", bad, "--reason", "r")
        assert exc.value.code == 2


# ---- registry ----------------------------------------------------------------------------


class TestRegistryTrials:
    def _idea(self, base_dir):
        return _write_idea(base_dir, "idea-001-sample", VALID)

    def test_valid_trials_yaml_is_accepted(self, registry_base):  # noqa: F811
        folder = self._idea(registry_base)
        trials_mod.add_external(folder, 2, "notebook sweep", now=NOW)
        assert [i["id"] for i in br.load_ideas(registry_base)] == ["IDEA-001"]

    def test_absent_trials_yaml_is_fine(self, registry_base):  # noqa: F811
        self._idea(registry_base)
        assert len(br.load_ideas(registry_base)) == 1

    def test_corrupt_trials_yaml_is_a_registry_problem(self, registry_base):  # noqa: F811
        folder = self._idea(registry_base)
        (folder / "trials.yaml").write_text(
            "- trial: 1\n  kind: bogus\n  created_ist: x\n", encoding="utf-8"
        )
        with pytest.raises(br.RegistryError, match=r"idea-001-sample: trials\.yaml: .*bogus"):
            br.load_ideas(registry_base)

    def test_unparseable_trials_yaml_is_a_registry_problem(self, registry_base):  # noqa: F811
        folder = self._idea(registry_base)
        (folder / "trials.yaml").write_text("- [unclosed\n", encoding="utf-8")
        with pytest.raises(br.RegistryError, match=r"trials\.yaml"):
            br.load_ideas(registry_base)


class TestLedgerShrank:
    def _problems(self, root_):
        folder = root_ / IDEA_DIR
        runs, problems = br.load_runs(folder)
        assert problems == []
        return br._trials_problems(folder, runs)

    def test_intact_ledger_has_no_problem(self, root):
        _record(root, external_trials=2, external_reason="sweep")
        assert self._problems(root) == []

    def test_removed_rows_are_flagged(self, root):
        _record(root, external_trials=2, external_reason="sweep")
        text = _trials_path(root).read_text(encoding="utf-8")
        _trials_path(root).write_text(text.split("- trial: 2")[0], encoding="utf-8")
        problems = self._problems(root)
        assert len(problems) == 1 and "trials.yaml shrank" in problems[0]

    def test_lowered_count_is_flagged(self, root):
        _record(root, external_trials=5, external_reason="sweep")
        text = _trials_path(root).read_text(encoding="utf-8")
        _trials_path(root).write_text(text.replace("count: 5", "count: 1"), encoding="utf-8")
        assert "trials.yaml shrank" in self._problems(root)[0]

    def test_deleted_ledger_is_flagged(self, root):
        _record(root)
        _trials_path(root).unlink()
        assert "trials.yaml shrank" in self._problems(root)[0]

    def test_growth_is_fine(self, root):
        _record(root)
        trials_mod.add_external(root / IDEA_DIR, 3, "later sweep", now=NOW)
        assert self._problems(root) == []

    def test_corrupt_ledger_reports_once_not_as_shrunk(self, root):
        _record(root)
        _trials_path(root).write_text("- trial: 1\n  kind: bogus\n  created_ist: x\n")
        problems = self._problems(root)
        assert len(problems) == 1 and "shrank" not in problems[0]


# ---- evaluators --------------------------------------------------------------------------


LEDGER_TRIALS = {"material": 2, "external": 3, "total": 5, "source": "trials.yaml"}


class TestG02Evaluator:
    def test_absent_is_not_evaluated(self):
        assert _row(_evaluate(_book()), "G0.2")["status"] == "not_evaluated"

    def test_ok_with_a_checked_symbol_passes(self):
        row = _row(_evaluate(_book(), truncation=OK_TRUNCATION), "G0.2")
        assert row["status"] == "pass"

    def test_ok_but_nothing_checked_does_not_pass(self):
        empty = {"ok": True, "checked": [], "mismatches": [], "skipped": [{"symbol": "A"}]}
        row = _row(_evaluate(_book(), truncation=empty), "G0.2")
        assert row["status"] != "pass"

    def test_mismatch_fails_and_names_the_first(self):
        row = _row(_evaluate(_book(), truncation=BAD_TRUNCATION), "G0.2")
        assert row["status"] == "fail"
        assert "A" in row["evidence"] and "trade_differs" in row["evidence"]
        assert "2026-01-10" in row["evidence"]

    def test_nothing_compared_is_not_evaluated(self):
        zero = {**OK_TRUNCATION, "compared_total": 0}
        row = _row(_evaluate(_book(), truncation=zero), "G0.2")
        assert row["status"] == "not_evaluated"
        assert "nothing was compared" in row["evidence"]

    def test_something_compared_passes(self):
        some = {**OK_TRUNCATION, "compared_total": 7}
        assert _row(_evaluate(_book(), truncation=some), "G0.2")["status"] == "pass"

    def test_mismatch_beats_nothing_compared(self):
        bad = {**BAD_TRUNCATION, "compared_total": 0}
        assert _row(_evaluate(_book(), truncation=bad), "G0.2")["status"] == "fail"

    def test_malformed_result_is_not_a_pass(self):
        row = _row(_evaluate(_book(), truncation={"nonsense": 1}), "G0.2")
        assert row["status"] != "pass"


class TestG25Evaluator:
    def test_count_without_a_ledger_block_fails(self):
        # a recorded run has n_trials > 0; without the trials block it was not ledger-derived
        row = _row(_evaluate(_book(), n_trials=1), "G2.5")
        assert row["status"] == "fail"
        assert "trials.yaml" in row["evidence"]

    def test_neither_count_nor_ledger_is_not_evaluated(self):
        assert _row(_evaluate(_book(), n_trials=0), "G2.5")["status"] == "not_evaluated"

    def test_ledger_derived_count_passes(self):
        row = _row(_evaluate(_book(), n_trials=5, trials=LEDGER_TRIALS), "G2.5")
        assert row["status"] == "pass" and row["value"] == 5
        assert "trials.yaml" in row["evidence"]

    def test_count_not_from_the_ledger_fails(self):
        guessed = {**LEDGER_TRIALS, "source": "caller"}
        row = _row(_evaluate(_book(), n_trials=5, trials=guessed), "G2.5")
        assert row["status"] == "fail"

    def test_count_disagreeing_with_the_run_fails(self):
        row = _row(_evaluate(_book(), n_trials=1, trials=LEDGER_TRIALS), "G2.5")
        assert row["status"] == "fail"

    def test_dsr_and_hurdle_use_the_ledger_number(self):
        book = [
            _trade(d, r_net=0.1 + 0.4 * (i % 3), r_gross=0.5 + 0.4 * (i % 3))
            for i, d in enumerate(_days(300))
        ]
        out = _evaluate(book, n_trials=5, trials=LEDGER_TRIALS)
        assert "5 trials" in _row(out, "G2.2")["threshold"]
        assert "5 trials" in _row(out, "G2.3")["evidence"]


# ---- the cap lift ------------------------------------------------------------------------

#: Backtest-stage gates (G0 to G6) the engine still cannot evaluate in a real run. They keep
#: the verdict at MARGINAL until their evidence exists; the list is pinned so a gate that
#: gains an evaluator (or loses one) is a deliberate change.
STILL_NOT_EVALUATED = frozenset(
    {
        "G0.1",
        "G0.7",
        "G4.2",
        "G4.8",
        "G5.1",
        "G5.3",
        "G5.4",
        "G5.5",
        "G6.6",
    }
)


def _everything_the_engine_can_evaluate_passes() -> dict:
    """An evaluation block in which every gate that has an evaluator passes.

    Gates without an evaluator are marked not_applicable: the fixture stands for the day
    their evidence exists, so the only thing left to hold the verdict is the readiness table.
    """
    dims: dict[str, list] = {name: [] for name in ev.DIMENSIONS}
    for gate, dimension, title, hard, _why in ev._GATES:
        status = "pass" if gate in ev._EVALUATORS else "not_applicable"
        dims[dimension].append(
            {
                "gate": gate,
                "title": title,
                "status": status,
                "hard": hard,
                "value": None,
                "threshold": "",
                "evidence": "",
            }
        )
    return {"dimensions": [{"name": n, "rows": r} for n, r in dims.items()]}


class TestCapLift:
    VALID = {"ok": True, "failures": [], "checks_run": ["data_quality", "config", "lineage"]}
    DQ = {"status": "PASS", "affects_validity": False, "items": []}

    def _derive(self, missing):
        return verdict_mod.derive(
            _everything_the_engine_can_evaluate_passes(),
            validity=self.VALID,
            data_quality=self.DQ,
            readiness_missing=missing,
        )

    def test_everything_passing_and_nothing_missing_is_edge(self):
        out = self._derive([])
        assert out["run_verdict"] == "EDGE"
        assert out["caps_applied"] == []

    def test_one_missing_readiness_row_holds_it_at_marginal(self):
        out = self._derive(["Point-in-time universe membership"])
        assert out["run_verdict"] == "MARGINAL"
        assert out["caps_applied"] == ["engine readiness: Point-in-time universe membership"]

    def test_promotion_is_not_ready_while_readiness_is_missing(self):
        evaluation = _everything_the_engine_can_evaluate_passes()
        held = promo.evaluate_promotion(
            evaluation,
            code_dirty=False,
            reproducible=True,
            readiness_missing=["Point-in-time universe membership"],
        )
        assert held["stages"]["backtest"]["decision"] == promo.NOT_READY
        assert any("engine readiness" in r for r in held["stages"]["backtest"]["reasons"])
        clear = promo.evaluate_promotion(
            evaluation, code_dirty=False, reproducible=True, readiness_missing=[]
        )
        assert clear["stages"]["backtest"]["decision"] == promo.READY

    def test_a_validity_failure_beats_a_clean_evaluation(self):
        bad = {**self.VALID, "ok": False, "failures": [{"check": "truncation", "detail": "x"}]}
        out = verdict_mod.derive(
            _everything_the_engine_can_evaluate_passes(),
            validity=bad,
            data_quality=self.DQ,
            readiness_missing=[],
        )
        assert out["run_verdict"] == "INCONCLUSIVE"

    def test_backtest_gates_without_an_evaluator_are_the_pinned_list(self):
        unevaluated = {
            g
            for g, *_ in ev._GATES
            if g not in ev._EVALUATORS and promo.gate_number(g) in promo.STAGE_GATES["backtest"]
        }
        assert unevaluated == STILL_NOT_EVALUATED


# ---- swing truncation through execute_backtest --------------------------------------------

SWING_DAYS = 90


class _SwingCausal(SwingScript):
    def __init__(self):
        super().__init__({})

    def prepare_for(self, symbol, df, p):
        out = df.copy()
        out["day"] = pd.Series(out.index.date, index=out.index)
        return out

    def entry_at_open(self, c, i, p, direction):
        if direction != 1 or i < 3:
            return None
        if float(c["Close"][i - 1]) > float(c["Close"][i - 2]) > float(c["Close"][i - 3]):
            o = float(c["Open"][i])
            return EntrySignal(1, o * 0.96, o * 1.04, "W")
        return None


class _SwingLookahead(_SwingCausal):
    """Entry decided by a whole-series statistic, so prepare_for depends on future bars."""

    def prepare_for(self, symbol, df, p):
        out = super().prepare_for(symbol, df, p)
        out["thr"] = out["Close"].mean()
        return out

    def entry_at_open(self, c, i, p, direction):
        if direction != 1 or i < 3 or float(c["Close"][i - 1]) <= float(c["thr"][i]):
            return None
        o = float(c["Open"][i])
        return EntrySignal(1, o * 0.96, o * 1.04, "W")


@dataclasses.dataclass
class _SwingParams:
    x: int = 1


def _swing_rows(seed):
    rng = np.random.default_rng(seed)
    px, rows = 100.0, []
    for _ in range(SWING_DAYS):
        o = px
        cl = o * (1 + rng.normal(0.001, 0.012))
        rows.append((o, max(o, cl) * 1.004, min(o, cl) * 0.996, cl))
        px = cl
    return rows


_SWING: dict = {}


@pytest.fixture(scope="module")
def swing_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("swing") / "d.duckdb"
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
        )
        rows = []
        for k, sym in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE")):
            for d, (o, h, lo, cl) in enumerate(_swing_rows(k)):
                rows.append((sym, "D", EPOCH0 + d * 86400, o, h, lo, cl, 1000))
        con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?)", rows)
    finally:
        con.close()
    return db


@pytest.fixture
def swing_env(swing_db, monkeypatch):
    from signal_engine.backtest import __main__ as cli
    from signal_engine.backtest import data
    from signal_engine.tests import test_evidence_ledgers as tel
    from signal_engine.tests.test_data_session_integration import _store_resolution

    real = data.from_historify_daily
    calls = []

    def spy(strategy, params, frames, run, *, engine=None, **kw):
        calls.append({"symbols": sorted(frames), "engine": engine, "run": run})
        return truncation_check(strategy, params, frames, run, engine=engine, **kw)

    monkeypatch.setattr(cli, "_register", lambda: None)
    # Plan Phase 4: the run reads bars only through its resolution (this test's own store).
    _SWING["res"] = _store_resolution(
        ("AAA", "BBB", "CCC", "DDD", "EEE"),
        daily=lambda **kw: real(db_path=swing_db, **{**kw, "min_sessions": 10}),
    )
    monkeypatch.setattr(rr, "random_entry_baseline", lambda *a, **k: None)
    monkeypatch.setattr(rr, "deployed_cost_model", tel.deployed_model)
    monkeypatch.setattr(rr, "truncation_check", spy)

    def use(strategy_cls):
        monkeypatch.setattr(cli, "REGISTRY", {"s": (strategy_cls, _SwingParams, "swing")})
        return calls

    return use


class TestSwingTruncation:
    def test_causal_swing_strategy_passes_with_the_swing_engine(self, swing_env):
        calls = swing_env(_SwingCausal)
        exe = rr.execute_backtest("s", settings=SETTINGS, dataset=_SWING["res"])
        assert len(calls) == 1
        assert calls[0]["engine"] is simulate_swing
        assert isinstance(calls[0]["run"], SwingRunConfig)
        assert len(calls[0]["symbols"]) <= 3
        assert exe.truncation is not None and exe.truncation["checked"]
        assert exe.truncation["ok"] is True, exe.truncation["mismatches"]
        assert exe.trades, "the fixture must produce trades or the check proves nothing"

    def test_lookahead_swing_strategy_fails(self, swing_env):
        swing_env(_SwingLookahead)
        exe = rr.execute_backtest("s", settings=SETTINGS, dataset=_SWING["res"])
        assert exe.truncation["ok"] is False
        assert exe.truncation["mismatches"]

    def test_skipped_swing_run_has_no_truncation(self, swing_env):
        calls = swing_env(_SwingCausal)
        exe = rr.execute_backtest("s", settings=SETTINGS, run_truncation=False, dataset=_SWING["res"])
        assert exe.truncation is None and calls == []
