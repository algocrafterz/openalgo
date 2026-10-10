"""G5.5 margin: worst simultaneous MIS margin of the capped book, as a share of capital."""

from __future__ import annotations

import copy
import dataclasses

import pandas as pd
import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import live_caps as lc
from signal_engine.backtest.perturbation import trade_metrics
from signal_engine.tests.cost_fixtures import make_model
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row

IST = "Asia/Kolkata"
MODEL = make_model()


def _trade(entry="09:20", exit_="10:00", entry_px=100.0, risk=1.0, day="2026-03-02", symbol="A.NS"):
    base = _noisy_book(n=1)[0]
    return dataclasses.replace(
        base, symbol=symbol,
        entry_time=pd.Timestamp(f"{day} {entry}", tz=IST), exit_time=pd.Timestamp(f"{day} {exit_}", tz=IST),
        entry=entry_px, signal_price=entry_px, sl=entry_px - risk, risk=risk,
    )  # fmt: skip


class TestPeakMargin:
    def test_one_position_uses_notional_over_leverage_in_units_of_capital(self):
        m = lc.peak_margin([_trade()], MODEL)  # 1% stop: notional = capital x 1% / 1% = capital
        assert m["peak_fraction"] == pytest.approx(1.0 / MODEL.leverage)
        assert m["positions_at_peak"] == 1 and m["trades"] == 1 and m["leverage"] == MODEL.leverage

    def test_overlapping_positions_add_and_sequential_ones_do_not(self):
        both = lc.peak_margin([_trade("09:20", "10:00"), _trade("09:30", "10:10", symbol="B.NS")], MODEL)
        seq = lc.peak_margin([_trade("09:20", "10:00"), _trade("10:10", "10:40", symbol="B.NS")], MODEL)
        assert both["peak_fraction"] == pytest.approx(2.0 / MODEL.leverage) and both["positions_at_peak"] == 2
        assert seq["peak_fraction"] == pytest.approx(1.0 / MODEL.leverage)

    def test_a_position_holds_margin_through_its_exit_bar(self):
        # the first exits on the 10:00 bar (held to 10:05); a fill on the 10:00 bar overlaps it
        overlap = lc.peak_margin([_trade("09:20", "10:00"), _trade("10:00", "10:30", symbol="B.NS")], MODEL)
        clear = lc.peak_margin([_trade("09:20", "10:00"), _trade("10:05", "10:30", symbol="B.NS")], MODEL)
        assert overlap["positions_at_peak"] == 2 and clear["positions_at_peak"] == 1

    def test_a_tight_stop_is_capped_by_leverage_at_all_of_the_capital(self):
        m = lc.peak_margin([_trade(risk=0.1)], MODEL)  # 0.1% stop: sized 10x capital, capped at 5x
        assert m["peak_fraction"] == pytest.approx(1.0)

    def test_it_does_not_depend_on_the_capital_level(self):
        trades = [_trade(), _trade("09:30", "10:10", symbol="B.NS")]
        double = dataclasses.replace(MODEL, capital=MODEL.capital * 2)
        assert lc.peak_margin(trades, double)["peak_fraction"] == pytest.approx(
            lc.peak_margin(trades, MODEL)["peak_fraction"], rel=1e-12)

    def test_no_trades_or_no_cost_model(self):
        assert lc.peak_margin([], MODEL)["peak_fraction"] == 0.0
        assert lc.peak_margin([_trade()], None) is None

    def test_the_peak_time_is_reported_in_ist(self):
        m = lc.peak_margin([_trade("09:20", "10:00")], MODEL)
        assert m["peak_time"].startswith("2026-03-02T09:20:00+05:30")


def _evidence(trades, primary=0.2, others=(0.3, 0.4), uncapped=40.0, state="complete", margin=True, seal=True):
    head = trade_metrics(trades)

    def row(slots, is_primary, peak):
        r = {"max_open_positions": slots, "primary": is_primary, "trades": 500, "net_r": 0.2,
             "net_bps": 20.0, "win_rate": 0.5, "peak_open_positions": slots,
             "vetoes": {"band": 0, "daily": 0, "weekly": 0, "monthly": 0, "slots": 0, "trades_day": 0},
             "trade_sha256": "x"}  # fmt: skip
        if margin:
            r["margin"] = {"peak_fraction": peak, "positions_at_peak": slots,
                           "peak_time": "2026-03-02T09:20:00+05:30", "leverage": 5.0, "trades": 500}  # fmt: skip
        return r

    body = {
        "version": 1, "state": state, "diagnostic_not_trials": True,
        "headline": {"trades": head["trades"], "net_r": head["net_r"], "net_bps": head["net_bps"],
                     "trade_sha256": lc.trade_hash(trades)},
        "base_check": {"trades": head["trades"], "net_r": head["net_r"], "net_bps": head["net_bps"],
                       "trade_sha256": "y",
                       "margin": {"peak_fraction": uncapped, "positions_at_peak": 60, "peak_time": None,
                                  "leverage": 5.0, "trades": head["trades"]}},
        "passes": [row(1, True, primary), row(2, False, others[0]), row(3, False, others[1])],
        "assumptions": ["a"], "limitations": ["l"],
    }  # fmt: skip
    return lc.seal_evidence(body) if seal else body


def _g55(trades, evidence):
    return _row(_evaluate(trades, live_caps=evidence), "G5.5")


class TestGate:
    def test_a_primary_book_inside_the_limit_passes(self):
        trades = _noisy_book()
        row = _g55(trades, _evidence(trades, primary=0.2))
        assert row["status"] == "pass" and row["hard"] is False and row["value"] == 0.2
        assert "20.0% of capital" in row["evidence"] and "not the broker's margin" in row["evidence"]

    def test_the_limit_is_inclusive_at_ninety_percent(self):
        trades = _noisy_book()
        assert _g55(trades, _evidence(trades, primary=0.9))["status"] == "pass"
        assert _g55(trades, _evidence(trades, primary=0.9001))["status"] == "fail"

    def test_a_primary_over_the_limit_fails(self):
        trades = _noisy_book()
        assert _g55(trades, _evidence(trades, primary=1.0))["status"] == "fail"

    def test_a_sensitivity_over_the_limit_is_flagged_but_not_decisive(self):
        trades = _noisy_book()
        row = _g55(trades, _evidence(trades, primary=0.2, others=(0.6, 1.4)))
        assert row["status"] == "pass"
        assert "3 slots: 140.0% (OVER the limit)" in row["evidence"] and "not decisive" in row["evidence"]

    def test_the_uncapped_book_is_information_only(self):
        trades = _noisy_book()
        text = _g55(trades, _evidence(trades, uncapped=62.0))["evidence"]
        assert "uncapped book would peak at 6200%" in text and "information only" in text

    def test_without_the_live_caps_stage_it_is_not_evaluated(self):
        row = _row(_evaluate(_noisy_book()), "G5.5")
        assert row["status"] == "not_evaluated" and "--live-caps" in row["evidence"]

    def test_evidence_from_before_the_margin_figures_existed_is_not_evaluated(self):
        trades = _noisy_book()
        row = _g55(trades, _evidence(trades, margin=False))
        assert row["status"] == "not_evaluated" and "rerun with --live-caps" in row["evidence"]

    def test_evidence_computed_from_another_run_is_not_evaluated(self):
        row = _g55(_noisy_book(seed=2), _evidence(_noisy_book(seed=1)))
        assert row["status"] == "not_evaluated" and "different run" in row["evidence"]

    @pytest.mark.parametrize("state", ["error", "unsupported", "base_replay_mismatch"])
    def test_a_stage_that_did_not_complete_is_not_evaluated(self, state):
        trades = _noisy_book()
        bad = _evidence(trades, state=state, seal=False)
        bad["reason"] = "why"
        assert _g55(trades, lc.seal_evidence(bad))["status"] == "not_evaluated"

    def test_same_count_and_same_mean_is_not_enough_to_identify_the_run(self):
        a = _noisy_book(seed=1)
        swapped = list(a)  # the same R values on different trades: same count, same mean net R
        swapped[0], swapped[1] = (dataclasses.replace(a[0], r_net=a[1].r_net),
                                  dataclasses.replace(a[1], r_net=a[0].r_net))
        assert trade_metrics(a)["trades"] == trade_metrics(swapped)["trades"]
        assert trade_metrics(a)["net_r"] == pytest.approx(trade_metrics(swapped)["net_r"], abs=1e-12)
        row = _g55(swapped, _evidence(a))
        assert row["status"] == "not_evaluated" and "different run" in row["evidence"]

    def test_an_edited_figure_breaks_the_seal(self):
        trades = _noisy_book()
        edited = copy.deepcopy(_evidence(trades, primary=0.95))
        edited["passes"][0]["margin"]["peak_fraction"] = 0.2  # a fail edited into a pass
        row = _g55(trades, edited)
        assert row["status"] == "not_evaluated" and "content_sha256" in row["evidence"]

    def test_unsealed_or_unbound_evidence_is_not_evaluated(self):
        trades = _noisy_book()
        assert _g55(trades, _evidence(trades, seal=False))["status"] == "not_evaluated"
        unbound = _evidence(trades, seal=False)
        del unbound["headline"]["trade_sha256"]
        assert _g55(trades, lc.seal_evidence(unbound))["status"] == "not_evaluated"

    def test_a_margin_not_computed_from_the_primary_trade_list_is_not_evaluated(self):
        trades = _noisy_book()
        bad = _evidence(trades, seal=False)
        bad["passes"][0]["margin"]["trades"] = 499
        row = _g55(trades, lc.seal_evidence(bad))
        assert row["status"] == "not_evaluated" and "own trade list" in row["evidence"]

    def test_the_seal_survives_a_json_round_trip_with_non_finite_numbers(self):
        import json

        trades = _noisy_book()
        body = _evidence(trades, seal=False)
        body["passes"][1]["net_r"] = float("nan")
        sealed = lc.seal_evidence(body)
        assert sealed["passes"][1]["net_r"] is None
        again = json.loads(json.dumps(sealed, allow_nan=False))
        assert lc.validate_evidence(again) == [] and again == sealed
        assert _g55(trades, again)["status"] == "pass"

    def test_malformed_margin_figures_are_not_evaluated(self):
        trades = _noisy_book()
        bad = _evidence(trades, seal=False)
        bad["passes"][0]["margin"] = {"peak_fraction": "x", "trades": 500}
        assert _g55(trades, lc.seal_evidence(bad))["status"] == "not_evaluated"

    def test_it_is_registered_and_a_failed_row_blocks_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert "G5.5" in ev._EVALUATORS
        for status in ("fail", "not_evaluated"):
            result = _derive(_evaluation("pass", G5_5=status))
            assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G5.5"


def test_the_margin_definition_and_primary_rule_are_stated_in_the_criteria_and_the_ledger():
    from pathlib import Path

    docs = Path(ev.__file__).parents[1] / "docs"
    criteria = (docs / "STRATEGY-ACCEPTANCE-CRITERIA.md").read_text(encoding="utf-8")
    ledger = (docs / "BACKTEST-ENGINE-READINESS.md").read_text(encoding="utf-8")
    row = next(ln for ln in criteria.splitlines() if ln.startswith("| G5.5 |"))
    line = next(ln for ln in ledger.splitlines() if ln.startswith("| G5.5 Margin |"))
    assert "one open position" in row and "not decisive" in row and "not the broker's margin" in row
    assert "90%" in line and "not decisive" in line and "not the broker's margin" in line
