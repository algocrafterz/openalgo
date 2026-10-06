"""The run record carries data-contract exclusions, the n_minutes mix, strategy counters and
the spec file's provenance. Without them the manifest is as before."""

from __future__ import annotations

import dataclasses

import pandas as pd

from signal_engine.backtest import __main__ as cli
from signal_engine.backtest import harness
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.types import Ctx
from signal_engine.tests import test_record_run as base
from signal_engine.tests.published_fixtures import frames_resolution
from signal_engine.tests.test_backtest_ledgers import FLAT, RUN, Script, make_frame
from signal_engine.tests.test_evidence_ledgers import deployed_model
from signal_engine.tests.test_run_artifact_write import (  # noqa: F401
    _folder,
    _manifest,
    _record,
    root,
)


def _execute_with(monkeypatch, strategy_cls):
    df = make_frame([("2026-06-04", [FLAT] * 6)])
    df["n_minutes"] = [5, 5, 4, 5, 1, 5]

    @dataclasses.dataclass
    class Params:
        x: int = 1

    class FakeBacktest:
        def __init__(self, strategy, frames, run, **kw):
            self.days = [pd.Timestamp("2026-06-04").date()]

        def trades(self, p, window="all", run=None, recorder=None):
            return simulate(Ctx(df, "AAA"), Script({}), None, RUN, rec=recorder)

    monkeypatch.setattr(cli, "_register", lambda: None)
    monkeypatch.setattr(cli, "REGISTRY", {"k": (strategy_cls, Params, "intraday")})
    monkeypatch.setattr(harness, "Backtest", FakeBacktest)
    monkeypatch.setattr(rr, "random_entry_baseline_intraday", lambda *a, **k: None)
    monkeypatch.setattr(rr, "deployed_cost_model", deployed_model)
    res = frames_resolution(intraday={"AAA": df})
    return rr.execute_backtest("k", dataset=res)


class TestExecutionCapturesDiagnostics:
    def test_the_n_minutes_mix_and_the_strategy_counters_are_captured(self, monkeypatch):
        class Strat:
            run_overrides: dict = {}

            def diagnostics(self):
                return {"arms": 4, "cap_blocks": 1}

        exe = _execute_with(monkeypatch, Strat)
        assert exe.strategy_diagnostics == {"arms": 4, "cap_blocks": 1}
        assert exe.n_minutes_distribution["overall"] == {"1": 1, "2": 0, "3": 0, "4": 1, "5": 4}
        assert exe.n_minutes_distribution["by_year"]["2026"]["5"] == 4

    def test_a_strategy_without_diagnostics_records_none(self, monkeypatch):
        class Strat:
            run_overrides: dict = {}

        assert _execute_with(monkeypatch, Strat).strategy_diagnostics is None


class TestManifest:
    def test_no_diagnostics_means_no_diagnostics_key(self, root):  # noqa: F811
        manifest = _manifest(_record(root))
        assert "run_diagnostics" not in manifest

    def test_diagnostics_are_recorded_in_the_manifest(self, root):  # noqa: F811
        exe = dataclasses.replace(
            base._execution(),
            n_minutes_distribution={"bars": 2, "overall": {"1": 1, "5": 1}, "by_year": {}},
            strategy_diagnostics={"arms": 2},
        )
        manifest = _manifest(_record(root, execute=lambda key: exe))
        diag = manifest["run_diagnostics"]
        assert diag["strategy_diagnostics"] == {"arms": 2}
        assert diag["n_minutes_distribution"]["bars"] == 2
        assert "data_contract_exclusions_by_year" not in diag

    def test_the_spec_file_sha256_is_recorded(self, root):  # noqa: F811
        import hashlib

        path = _record(root)
        spec = _folder(path).parents[1] / "spec.yaml"
        recorded = _manifest(path)["spec_source"]
        assert recorded["sha256"] == hashlib.sha256(spec.read_bytes()).hexdigest()
        assert recorded["commit"] is None and recorded["modified_since_commit"] is True


def test_data_contract_exclusions_by_year_come_from_the_preflight(tmp_path):
    from types import SimpleNamespace

    from signal_engine.backtest import run_artifact as ra

    by_year = {"2024": {"DATA-MISSING-BIN": 3}}
    res = SimpleNamespace(report=SimpleNamespace(
        universe={"session_exclusions_by_year": by_year}))
    out = ra._run_diagnostics_block(SimpleNamespace(), res, tmp_path)
    assert out == {"run_diagnostics": {"data_contract_exclusions_by_year": by_year}}
    assert ra._run_diagnostics_block(SimpleNamespace(), None, tmp_path) == {}
