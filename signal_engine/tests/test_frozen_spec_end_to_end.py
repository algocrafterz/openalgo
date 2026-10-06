"""The frozen spec is authoritative on the real run path.

`record_idea` freezes the spec and hands its intent to the real `execute_backtest`, which builds the
strategy, the run config and the cost model. Only the data layer, the strategy registry and the
cost-model profile are replaced; nothing is passed by hand. Each test changes the SPEC and checks
that the change shows up in what the engine and the strategy received.
"""

from __future__ import annotations

import pytest

from signal_engine.backtest import __main__ as cli
from signal_engine.backtest import data, harness
from signal_engine.backtest import record_run as rr
from signal_engine.research.intake import versions
from signal_engine.tests import test_evidence_ledgers as tel
from signal_engine.tests.published_fixtures import frames_resolution
from signal_engine.tests.ranked_fixtures import RankedTop, RankParams, ranked_frames
from signal_engine.tests.test_ranked_record_run import SPEC
from signal_engine.tests.test_spec_gate import _idea, _write_spec


class NoOverride(RankedTop):
    """No run_overrides: any unlimited holding period must come from the frozen spec."""

    run_overrides = {}


@pytest.fixture(autouse=True)
def _stub_provenance(monkeypatch):
    from signal_engine.backtest import manifest

    monkeypatch.setattr(
        manifest,
        "git_state",
        lambda root: {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []},
    )
    monkeypatch.setattr(manifest, "dataset_fingerprint", lambda symbols, interval, as_of: "d" * 64)


@pytest.fixture
def seen(monkeypatch):
    log: dict = {"backtests": 0, "executions": []}

    class Spy(harness.Backtest):
        def __init__(self, strategy, frames, run, **kw):
            log["run"] = run
            log["backtests"] += 1
            super().__init__(strategy, frames, run, **kw)

    class Strat(NoOverride):
        def prepare_panel(self, frames, p, calendar):
            log["params_seen_by_strategy"] = dict(vars(p)) if hasattr(p, "__dict__") else p
            return super().prepare_panel(frames, p, calendar)

    monkeypatch.setattr(cli, "_register", lambda: None)
    monkeypatch.setattr(cli, "REGISTRY", {"orb": (Strat, RankParams, "swing")})
    # Plan Phase 4: record_idea resolves the run's dataset; here it is the ranked panel itself.
    res = frames_resolution(daily=ranked_frames())
    monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: res)
    monkeypatch.setattr(harness, "Backtest", Spy)
    monkeypatch.setattr(rr, "deployed_cost_model", tel.deployed_model)
    return log


def real_execute(log):
    """The real execute_backtest, keeping each Execution it returns."""

    def execute(key, settings=None, end=None, run_truncation=True, dataset=None):
        exe = rr.execute_backtest(key, settings=settings, end=end, run_truncation=run_truncation,
                                  dataset=dataset)
        log["executions"].append(exe)
        return exe

    return execute


def record(tmp_path, seen, spec):
    folder = _idea(tmp_path, "012")
    _write_spec(folder, spec)
    path = rr.record_idea(
        tmp_path,
        "IDEA-012",
        execute=real_execute(seen),
        run_date="2026-10-04",
        supported={"orb"},
    )
    return folder, path


class TestFrozenSpecIsAuthoritative:
    def test_a_conforming_spec_runs_through_the_real_path(self, tmp_path, seen):
        _, path = record(tmp_path, seen, SPEC)
        assert path.exists() and seen["backtests"] >= 1

    def test_the_frozen_parameters_reach_the_strategy(self, tmp_path, seen):
        record(tmp_path, seen, SPEC)
        got = seen["params_seen_by_strategy"]
        assert (got["n"], got["closeness"], got["window"]) == (3, 0.9, 20)

    def test_the_frozen_parameters_are_what_the_run_records(self, tmp_path, seen):
        record(tmp_path, seen, SPEC)
        assert seen["executions"][-1].params["strategy"] == SPEC["parameters"]

    @pytest.mark.parametrize("capital", [100000, 250000])
    def test_fixed_notional_comes_from_the_frozen_spec(self, tmp_path, seen, capital):
        spec = {**SPEC, "portfolio": {**SPEC["portfolio"], "capital_per_position": capital}}
        record(tmp_path, seen, spec)
        assert seen["executions"][-1].cost_model.fixed_notional == float(capital)

    def test_an_unlimited_holding_period_is_honoured(self, tmp_path, seen):
        record(tmp_path, seen, SPEC)
        assert seen["run"].max_hold_bars is None

    def test_a_finite_holding_period_is_not_unlimited(self, tmp_path, seen):
        spec = {**SPEC, "holding_period": "12 bars"}
        record(tmp_path, seen, spec)
        assert seen["run"].max_hold_bars == 120  # the engine default

    def test_a_spec_whose_parameters_differ_from_the_class_fails_the_run(self, tmp_path, seen):
        spec = {**SPEC, "parameters": {**SPEC["parameters"], "closeness": 0.8}}
        folder = _idea(tmp_path, "012")
        _write_spec(folder, spec)
        with pytest.raises(rr.ParameterBindingError, match="closeness"):
            rr.record_idea(
                tmp_path,
                "IDEA-012",
                execute=real_execute(seen),
                run_date="2026-10-04",
                supported={"orb"},
            )
        assert seen["backtests"] == 0
        assert not (folder / "runs").exists()

    def test_a_failed_binding_leaves_no_registered_version(self, tmp_path, seen):
        spec = {**SPEC, "parameters": {**SPEC["parameters"], "n": 7}}
        folder = _idea(tmp_path, "012")
        _write_spec(folder, spec)
        with pytest.raises(rr.ParameterBindingError):
            rr.record_idea(
                tmp_path,
                "IDEA-012",
                execute=real_execute(seen),
                run_date="2026-10-04",
                supported={"orb"},
            )
        assert not (folder / "versions.yaml").exists()
        assert versions.load_versions(folder) == []
