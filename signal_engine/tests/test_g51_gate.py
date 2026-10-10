"""G5.1 evaluator and its opt-in attachment: Monte Carlo evidence bound to the run's own trades."""

from __future__ import annotations

import copy
import dataclasses
import json

import numpy as np
import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import monte_carlo as mc
from signal_engine.backtest import record_run as rr
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row

F = 0.01
PROFILE = {"risk_per_trade": F, "mean_total_bps": 10.0, "flat_bps": 16.0, "product": "MIS",
           "exchange": "NSE", "capital": 1e5, "leverage": 5.0, "slippage_bps_per_side": 4.0,
           "total_bps_at": {"1.0": 10.0, "1.5": 12.0, "2.0": 14.0}, "net_bps": 5.0,
           "net_bps_slip_2x": 3.0}  # fmt: skip


def _book(mean=0.3, n=400, seed=1):
    return _noisy_book(n=n, mean=mean, seed=seed)


def _attach(trades, f=F, paths=5000, **kw):
    return mc.attached_evidence(trades, f, n_paths=paths, **kw)


def _g51(trades, evidence, profile=PROFILE):
    return _row(_evaluate(trades, monte_carlo=evidence, cost_profile=profile), "G5.1")


def _reseal(evidence, edit):
    body = copy.deepcopy({k: v for k, v in evidence.items() if k != "content_sha256"})
    edit(body["report"])
    return {**body, "content_sha256": mc.seal(body)}


@pytest.fixture(scope="module")
def good():
    trades = _book(mean=0.3)
    return trades, _attach(trades)


class TestG51Evaluator:
    def test_without_the_stage_it_is_not_evaluated(self):
        row = _row(_evaluate(_book()), "G5.1")
        assert row["status"] == "not_evaluated" and "opt-in" in row["evidence"]

    def test_a_benign_book_passes_on_both_methods(self, good):
        trades, evidence = good
        row = _g51(trades, evidence)
        assert row["status"] == "pass" and row["hard"] is False
        assert "day:" in row["evidence"] and "trade:" in row["evidence"]
        assert "no portfolio cap" in row["evidence"] and "nothing about G5.2" in row["evidence"]

    def test_a_losing_book_fails(self):
        trades = _book(mean=-0.6)
        row = _g51(trades, _attach(trades))
        assert row["status"] == "fail" and row["value"] > ev.MC_MAX_P95_DRAWDOWN

    def test_evidence_from_another_trade_list_is_not_evaluated(self, good):
        _, evidence = good
        row = _g51(_book(mean=0.3, seed=2), evidence)
        assert row["status"] == "not_evaluated" and "different trade list" in row["evidence"]

    def test_a_different_risk_per_trade_is_not_evaluated(self, good):
        trades, _ = good
        row = _g51(trades, _attach(trades, f=0.005))
        assert row["status"] == "not_evaluated" and "risk per trade" in row["evidence"]

    def test_too_few_paths_is_not_evaluated(self):
        trades = _book()
        row = _g51(trades, _attach(trades, paths=1000))
        assert row["status"] == "not_evaluated" and "fewer than 5,000" in row["evidence"]

    def test_tampered_evidence_is_not_evaluated(self, good):
        trades, evidence = good
        bad = json.loads(json.dumps(evidence))
        bad["report"]["parameters"]["paths"] = 99
        row = _g51(trades, bad)
        assert row["status"] == "not_evaluated" and "invalid" in row["evidence"]

    def test_an_error_stub_is_not_evaluated_with_its_reason(self, good):
        trades, _ = good
        stub = {"schema": mc.SCHEMA, "state": "error", "reason": "no trade has a defined R"}
        row = _g51(trades, stub)
        assert row["status"] == "not_evaluated" and "no trade has a defined R" in row["evidence"]

    def test_trades_without_a_defined_r_make_it_not_evaluated(self):
        trades = _book()
        trades[0] = dataclasses.replace(trades[0], r_gross=float("nan"), r_net=float("nan"))
        row = _g51(trades, _attach(trades))
        assert row["status"] == "not_evaluated" and "no defined R" in row["evidence"]

    def test_no_recorded_risk_per_trade_is_not_evaluated(self, good):
        trades, evidence = good
        row = _g51(trades, evidence, profile=None)
        assert row["status"] == "not_evaluated"

    def test_the_drawdown_boundary_passes_and_just_above_fails(self, good):
        trades, evidence = good

        def set_dd(value):
            def edit(report):
                for m in ev.MC_METHODS:
                    report["methods"][m]["c2_account"][f"{F:g}"]["max_drawdown_fraction"]["p95"] = value
            return _reseal(evidence, edit)

        assert _g51(trades, set_dd(0.25))["status"] == "pass"
        assert _g51(trades, set_dd(0.2501))["status"] == "fail"

    def test_the_ruin_boundary_fails_at_five_percent_and_passes_just_below(self, good):
        trades, evidence = good

        def set_ruin(value):
            def edit(report):
                report["methods"]["trade"]["c2_account"][f"{F:g}"]["p_ruin"]["p"] = value
            return _reseal(evidence, edit)

        assert _g51(trades, set_ruin(0.05))["status"] == "fail"
        assert _g51(trades, set_ruin(0.0499))["status"] == "pass"

    def test_one_failing_method_fails_the_gate(self, good):
        trades, evidence = good

        def edit(report):
            report["methods"]["day"]["c2_account"][f"{F:g}"]["p_ruin"]["p"] = 0.2

        assert _g51(trades, _reseal(evidence, edit))["status"] == "fail"

    def test_a_different_ruin_definition_is_not_evaluated(self, good):
        trades, evidence = good

        def edit(report):
            report["parameters"]["theta"] = 0.5

        row = _g51(trades, _reseal(evidence, edit))
        assert row["status"] == "not_evaluated" and "theta" in row["evidence"]

    def test_missing_or_unreadable_figures_are_not_evaluated_not_a_crash(self, good):
        trades, evidence = good

        def drop(report):
            del report["methods"]["day"]["c2_account"][f"{F:g}"]["max_drawdown_fraction"]["p95"]

        def blank(report):
            report["methods"]["trade"]["c2_account"][f"{F:g}"]["p_ruin"]["p"] = None

        for edit in (drop, blank):
            row = _g51(trades, _reseal(evidence, edit))
            assert row["status"] == "not_evaluated" and "unreadable" in row["evidence"]

    def test_a_malformed_record_is_not_evaluated(self, good):
        trades, evidence = good
        assert _g51(trades, "text")["status"] == "not_evaluated"
        assert _g51(trades, {**evidence, "source": None})["status"] == "not_evaluated"

    def test_evidence_without_a_trade_binding_is_not_evaluated(self, good):
        trades, evidence = good

        body = copy.deepcopy({k: v for k, v in evidence.items() if k != "content_sha256"})
        body["source"] = {"kind": "recorded-run-trades"}
        resealed = {**body, "content_sha256": mc.seal(body)}
        assert _g51(trades, resealed)["status"] == "not_evaluated"

    def test_the_evidence_and_the_criteria_state_the_uncapped_scope(self, good):
        from pathlib import Path

        trades, evidence = good
        text = _g51(trades, evidence)["evidence"]
        assert "no portfolio cap applied" in text and "nothing about G5.2" in text
        criteria = (Path(ev.__file__).parents[1] / "docs" / "STRATEGY-ACCEPTANCE-CRITERIA.md").read_text(
            encoding="utf-8"
        )
        line = next(ln for ln in criteria.splitlines() if ln.startswith("| G5.1 |"))
        assert "without portfolio caps" in line and "G5.2" in line and "75% of the start" in line
        assert any("portfolio caps are not applied" in t for t in evidence["report"]["limitations"])

    def test_it_is_registered_and_a_not_evaluated_row_blocks_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert "G5.1" in ev._EVALUATORS
        result = _derive(_evaluation("pass", G5_1="not_evaluated"))
        assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G5.1"


class TestAttachedEvidence:
    def test_it_names_the_trade_list_and_the_risk_it_was_computed_for(self, good):
        trades, evidence = good
        src = evidence["source"]
        assert src["kind"] == "recorded-run-trades" and src["trades"] == len(trades)
        assert src["trade_fingerprint"] == mc.trade_fingerprint(trades)
        assert src["deployed_f"] == F and evidence["diagnostic_not_trials"] is True
        assert mc.validate_evidence(evidence) == []

    def test_the_fingerprint_ignores_order_and_sees_a_changed_r(self):
        trades = _book(n=50)
        assert mc.trade_fingerprint(trades) == mc.trade_fingerprint(trades[::-1])
        changed = [dataclasses.replace(trades[0], r_net=trades[0].r_net + 0.01), *trades[1:]]
        assert mc.trade_fingerprint(changed) != mc.trade_fingerprint(trades)

    def test_it_is_deterministic(self):
        trades = _book(n=100)
        assert _attach(trades) == _attach(trades)

    def test_a_deployed_risk_outside_the_default_list_is_simulated(self):
        trades = _book(n=100)
        report = _attach(trades, f=0.02)["report"]
        assert "0.02" in report["methods"]["day"]["c2_account"]
        assert report["methods"]["day"]["c2_account"]["0.02"]["deployed_reference"] is True

    def test_the_standalone_diagnostic_output_is_unchanged_by_the_new_parameter(self):
        frame = mc.frame_from_trades(_book(n=100))
        a = mc.build_report(frame, n_paths=500)
        assert a["parameters"]["deployed_reference_fraction"] == mc.DEPLOYED_F
        assert a["parameters"]["risk_fractions"] == list(mc.RISK_FRACTIONS)


class TestStageInRecording:
    def test_the_stage_builds_evidence_from_the_run_trades_and_cost_model(self):
        from signal_engine.tests.test_record_run import _execution

        execution = _execution()
        out = rr._monte_carlo_stage(execution)
        assert out["source"]["deployed_f"] == execution.cost_model.risk_per_trade
        assert out["source"]["trade_fingerprint"] == mc.trade_fingerprint(execution.trades)

    def test_a_trade_list_with_no_defined_r_gives_an_error_stub(self):
        from signal_engine.tests.test_record_run import _execution

        execution = _execution()
        nan = float("nan")
        trades = [dataclasses.replace(t, r_gross=nan, r_net=nan) for t in execution.trades]
        out = rr._monte_carlo_stage(dataclasses.replace(execution, trades=trades))
        assert out["state"] == "error"

    def test_execute_frozen_attaches_only_when_asked(self):
        from types import SimpleNamespace

        from signal_engine.tests.test_record_run import _execution

        frozen = SimpleNamespace(resolved={"execution.cost_bps_round_trip": {"value": 16.0}},
                                 intent={}, profile={})
        plain = rr._execute_frozen(lambda key: _execution(), "k", frozen)
        asked = rr._execute_frozen(lambda key: _execution(), "k", frozen, run_monte_carlo=True)
        assert plain.monte_carlo is None and asked.monte_carlo["source"]["kind"] == "recorded-run-trades"


def test_the_cli_flag_exists():
    import inspect

    assert "run_monte_carlo" in inspect.signature(rr.record_idea).parameters
    assert "--monte-carlo" in inspect.getsource(rr.main)
