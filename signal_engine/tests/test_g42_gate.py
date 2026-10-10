"""G4.2 latency stage and gate: the headline book against the same book filled one bar later."""

from __future__ import annotations

import copy

import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import latency
from signal_engine.backtest.live_caps import trade_hash
from signal_engine.backtest.perturbation import trade_metrics
from signal_engine.tests.cost_fixtures import make_model
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row
from signal_engine.tests.test_perturbation import _frames, _headline, _Long, _P, _run


def _stage(frames, headline=None, **kw):
    model, run, strategy, params = make_model(), _run(), kw.pop("strategy", _Long()), _P()
    head = headline if headline is not None else _headline(strategy, params, frames, run, model)
    calls = []

    def load(chunk):
        calls.append(list(chunk))
        return {s: frames[s].copy() for s in chunk}

    out = latency.run_stage(
        strategy=strategy, params=params, load_frames=kw.pop("load", load), symbols=list(frames),
        run=run, cost_model=model, headline_trades=head, oos_start="2026-02-20",
        shared={"bar_size": "5m"}, chunk_size=2,
    )  # fmt: skip
    return out, calls, head


class TestStage:
    def test_a_complete_stage_replays_the_headline_and_delays_every_entry(self):
        out, calls, head = _stage(_frames())
        assert out["state"] == "complete" and out["base_replay_matches"] is True
        assert out["base"]["trade_sha256"] == trade_hash(head)
        assert out["delayed"]["trades"] == out["base"]["trades"] > 0
        assert out["delay_bars"] == 1 and out["diagnostic_not_trials"] is True
        assert len(calls) == 2  # three symbols, two per chunk

    def test_a_rising_market_makes_the_late_fill_worse(self):
        out, _, _ = _stage(_frames(drift_bps=60.0))
        assert out["delayed"]["net_r"] < out["base"]["net_r"]

    def test_it_is_deterministic_and_leaves_the_strategy_untouched(self):
        a, _, _ = _stage(_frames())
        b, _, _ = _stage(_frames())
        assert a == b

    def test_a_headline_from_other_data_stops_the_stage(self):
        out, _, _ = _stage(_frames(), headline=_headline(_Long(), _P(), _frames(drift_bps=10.0), _run(), make_model()))
        assert out["state"] == "base_replay_mismatch" and "does not equal the headline" in out["reason"]
        assert "delayed" not in out

    @pytest.mark.parametrize("change", ["reason", "drop", "shift_exit", "tiny_r", "other_symbol"])
    def test_any_difference_between_the_replay_and_the_headline_stops_the_stage(self, change):
        import dataclasses

        import pandas as pd

        head = list(_headline(_Long(), _P(), _frames(), _run(), make_model()))
        t = head[0]
        if change == "reason":
            head[0] = dataclasses.replace(t, reason="TP")
        elif change == "drop":
            head = head[1:]
        elif change == "shift_exit":
            head[0] = dataclasses.replace(t, exit_time=pd.Timestamp(t.exit_time) + pd.Timedelta(minutes=5))
        elif change == "tiny_r":
            head[0] = dataclasses.replace(t, r_net=t.r_net + 1e-6)
        else:
            head[0] = dataclasses.replace(t, symbol="ZZ")
        out, _, _ = _stage(_frames(), headline=head)
        assert out["state"] == "base_replay_mismatch", change

    def test_no_headline_trades_is_r_undefined(self):
        out, _, _ = _stage(_frames(), headline=[])
        assert out["state"] == "r_undefined"

    def test_a_failing_load_is_an_error_state_not_a_crash(self):
        def boom(chunk):
            raise RuntimeError("no bars")

        out, _, _ = _stage(_frames(), load=boom)
        assert out["state"] == "error" and "no bars" in out["reason"]

    def test_the_limitations_name_it_a_simulation_not_a_measurement(self):
        out, _, _ = _stage(_frames())
        assert any("not a measured latency" in t for t in out["limitations"])


def _evidence(trades, base_r=0.2, late_r=0.1, state="complete", delay=1, **extra):
    base = {**trade_metrics(trades), "trade_sha256": trade_hash(trades)}
    base["net_r"] = base_r
    body = {
        "version": 1, "state": state, "reason": "x", "delay_bars": delay,
        "diagnostic_not_trials": True, "limitations": ["l"], "base": base,
        "shared": {"bar_size": "5m"},
        "delayed": {"trades": len(trades), "net_r": late_r, "net_bps": 1.0},
    }  # fmt: skip
    body.update(extra)
    return body


def _g42(trades, evidence):
    return _row(_evaluate(trades, latency=evidence), "G4.2")


class TestGate:
    def test_a_surviving_sign_is_reported_without_a_flag(self):
        trades = _noisy_book()
        row = _g42(trades, _evidence(trades, 0.2, 0.1))
        assert row["status"] == "report" and row["hard"] is False and row["value"] == 0.1
        assert "does not flip" in row["evidence"] and "not a measured latency" in row["evidence"]

    def test_a_flipped_sign_is_flagged_and_still_only_a_report(self):
        trades = _noisy_book()
        row = _g42(trades, _evidence(trades, 0.2, -0.05))
        assert row["status"] == "report" and "SIGN FLIPS" in row["evidence"]

    def test_a_negative_headline_that_stays_negative_does_not_flip(self):
        trades = _noisy_book()
        assert "does not flip" in _g42(trades, _evidence(trades, -0.2, -0.3))["evidence"]

    def test_without_the_stage_it_is_not_evaluated(self):
        row = _row(_evaluate(_noisy_book()), "G4.2")
        assert row["status"] == "not_evaluated" and "opt-in" in row["evidence"]

    @pytest.mark.parametrize("state", ["unsupported", "base_replay_mismatch", "r_undefined", "error"])
    def test_a_stage_that_did_not_complete_is_not_evaluated(self, state):
        trades = _noisy_book()
        row = _g42(trades, _evidence(trades, state=state, reason=f"why {state}"))
        assert row["status"] == "not_evaluated" and state in row["evidence"]

    def test_a_delay_other_than_one_bar_is_not_evaluated(self):
        trades = _noisy_book()
        assert _g42(trades, _evidence(trades, delay=2))["status"] == "not_evaluated"

    def test_a_boolean_delay_is_not_taken_for_one_bar(self):
        trades = _noisy_book()
        assert _g42(trades, _evidence(trades, delay=True))["status"] == "not_evaluated"

    def test_evidence_that_does_not_state_five_minute_bars_is_not_evaluated(self):
        trades = _noisy_book()
        for shared in ({}, {"bar_size": "1m"}):
            row = _g42(trades, _evidence(trades, shared=shared))
            assert row["status"] == "not_evaluated" and "5-minute" in row["evidence"]

    def test_evidence_from_another_trade_list_is_not_evaluated(self):
        row = _g42(_noisy_book(seed=2), _evidence(_noisy_book(seed=1)))
        assert row["status"] == "not_evaluated" and "different trade list" in row["evidence"]

    def test_an_undefined_net_r_is_not_evaluated(self):
        trades = _noisy_book()
        evidence = _evidence(trades)
        evidence["delayed"]["net_r"] = None
        assert _g42(trades, evidence)["status"] == "not_evaluated"

    def test_invalid_or_malformed_evidence_is_not_evaluated(self):
        trades = _noisy_book()
        bad = copy.deepcopy(_evidence(trades))
        bad["diagnostic_not_trials"] = False
        assert _g42(trades, bad)["status"] == "not_evaluated"
        assert _g42(trades, "text")["status"] == "not_evaluated"
        assert _g42(trades, {**_evidence(trades), "state": "nope"})["status"] == "not_evaluated"

    def test_it_is_registered_and_a_not_evaluated_row_blocks_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert "G4.2" in ev._EVALUATORS
        result = _derive(_evaluation("pass", G4_2="not_evaluated"))
        assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G4.2"
        assert _derive(_evaluation("pass", G4_2="report"))["run_verdict"] == vd.EDGE


def test_the_record_flag_and_stage_hooks_exist():
    import inspect

    from signal_engine.backtest import record_run as rr

    assert "run_latency" in inspect.signature(rr.record_idea).parameters
    assert "--latency" in inspect.getsource(rr.main)
    assert rr._latency_stage(supported=False, trades=[], days=[])["state"] == "unsupported"
