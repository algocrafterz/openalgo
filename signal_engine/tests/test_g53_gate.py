"""G5.3 sizing: risk per trade at most 1% of capital and at most half of the estimated Kelly."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest.evaluation import kelly_fraction
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row, _t


def _profile(risk=0.01):
    return {"risk_per_trade": risk, "mean_total_bps": 10.0, "flat_bps": 16.0, "product": "MIS",
            "exchange": "NSE", "capital": 1e5, "leverage": 5.0, "slippage_bps_per_side": 4.0,
            "total_bps_at": {"1.0": 10.0, "1.5": 12.0, "2.0": 14.0}, "net_bps": 5.0,
            "net_bps_slip_2x": 3.0}  # fmt: skip


def _coin_book(wins, n=600, start="2020-01-01"):
    """One trade per session at +1R or -1R: Kelly is exactly 2p - 1 per trade and per session."""
    import pandas as pd

    days = [d.date().isoformat() for d in pd.bdate_range(start, periods=n)]
    return [_t(d, 1.0 if i < wins else -1.0) for i, d in enumerate(days)]


def _g53(trades, risk=0.01, profile="default"):
    prof = _profile(risk) if profile == "default" else profile
    return _row(_evaluate(trades, cost_profile=prof), "G5.3")


class TestKelly:
    def test_it_matches_the_textbook_cases(self):
        assert kelly_fraction([1.0] * 60 + [-1.0] * 40) == pytest.approx(0.2, abs=1e-6)
        assert kelly_fraction([2.0] * 50 + [-1.0] * 50) == pytest.approx(0.25, abs=1e-6)

    def test_no_positive_mean_means_no_bet(self):
        assert kelly_fraction([1.0, -1.0]) == 0.0
        assert kelly_fraction([-1.0, 0.5, -2.0]) == 0.0

    def test_a_big_loss_keeps_the_fraction_short_of_ruin(self):
        f = kelly_fraction([3.0, -1.0, -3.0, 3.0])
        assert 0 < f < 1 / 3

    def test_it_refuses_unusable_input(self):
        for bad in ([], [1.0, float("nan")], [1.0, float("inf")], [-1.0, float("-inf")]):
            with pytest.raises(ValueError):
                kelly_fraction(bad)

    def test_a_positive_mean_with_no_losing_outcome_is_unbounded_and_refused(self):
        for no_loss in ([0.5] * 10, [0.0, 1.0, 2.0]):  # zero variance and mixed
            with pytest.raises(ValueError, match="unbounded"):
                kelly_fraction(no_loss)

    def test_zero_variance_without_an_edge_is_zero_not_an_error(self):
        assert kelly_fraction([0.0] * 10) == 0.0
        assert kelly_fraction([-0.5] * 10) == 0.0

    def test_extreme_values_stay_finite_and_inside_the_ruin_bound(self):
        huge_loss = kelly_fraction([1e6] * 9 + [-1e3])
        assert 0 < huge_loss < 1e-3
        huge_win = kelly_fraction([1e9, -1.0, -1.0])
        assert 0 < huge_win < 1.0
        assert np.isfinite(kelly_fraction([5.0] * 30 + [-50.0]))


class TestG53Evaluator:
    def test_a_book_with_a_real_edge_passes(self):
        row = _g53(_coin_book(wins=390))  # Kelly 30%
        assert row["status"] == "pass" and row["hard"] is False
        assert 0 < row["value"] < 0.5 and "estimated Kelly" in row["evidence"]

    def test_risk_above_one_percent_fails_even_with_a_huge_edge(self):
        row = _g53(_coin_book(wins=390), risk=0.02)
        assert row["status"] == "fail" and "(over)" in row["evidence"]

    def test_half_kelly_is_inclusive(self):
        book = _coin_book(wins=306)  # p = 0.51, Kelly = 2%, half = 1%
        assert _g53(book, risk=0.01)["status"] == "pass"
        assert _g53(book, risk=0.0101)["status"] == "fail"

    def test_a_thin_edge_fails_the_kelly_share(self):
        row = _g53(_coin_book(wins=303))  # p = 0.505, Kelly 1%, half 0.5% < 1%
        assert row["status"] == "fail" and row["value"] > 0.5

    def test_a_book_without_an_edge_fails_with_the_reason(self):
        row = _g53(_noisy_book(mean=-0.3))
        assert row["status"] == "fail" and "no positive edge" in row["evidence"] and row["value"] is None

    def test_the_smaller_of_the_per_trade_and_per_session_estimates_is_used(self):
        # three trades per session sharing one outcome: the per-session Kelly is the smaller
        days = _coin_book(wins=390)
        trades = []
        for t in days:
            trades += [t, dataclasses.replace(t, symbol="B.NS"), dataclasses.replace(t, symbol="C.NS")]
        row = _g53(trades)
        assert "per session, the smaller" in row["evidence"]

    def test_the_per_session_estimate_groups_by_ist_entry_date_and_counts_same_day_trades_together(self):
        # three identical trades per session: the session outcome is +-3R, so Kelly is a third
        book = _coin_book(wins=390)
        trades = []
        for t in book:
            trades += [t, dataclasses.replace(t, symbol="B.NS"), dataclasses.replace(t, symbol="C.NS")]
        from signal_engine.backtest import monte_carlo as mc

        sessions = mc.Sessions(mc.frame_from_trades(trades))
        assert sessions.n_days == 600 and set(sessions.counts) == {3}
        per_trade = kelly_fraction([t.r_net for t in trades])
        assert kelly_fraction(sessions.day_r) == pytest.approx(per_trade / 3, rel=1e-3)

    def test_sessions_are_dated_in_ist_not_utc(self):
        import pandas as pd

        from signal_engine.backtest import monte_carlo as mc

        base = _coin_book(wins=1, n=2)[0]
        # 00:30 IST on 6 Jan is 19:00 UTC on 5 Jan: still the IST session of 6 Jan
        early = dataclasses.replace(base, entry_time=pd.Timestamp("2026-01-06 00:30", tz="Asia/Kolkata"),
                                    exit_time=pd.Timestamp("2026-01-06 01:00", tz="Asia/Kolkata"))
        late = dataclasses.replace(base, entry_time=pd.Timestamp("2026-01-06 10:00", tz="Asia/Kolkata"),
                                   exit_time=pd.Timestamp("2026-01-06 11:00", tz="Asia/Kolkata"))
        assert mc.Sessions(mc.frame_from_trades([early, late])).n_days == 1

    def test_the_one_percent_cap_is_a_hard_boundary(self):
        book = _coin_book(wins=390)
        assert _g53(book, risk=0.01)["status"] == "pass"
        assert _g53(book, risk=0.010001)["status"] == "fail"

    def test_a_zero_mean_book_fails_and_a_tiny_positive_one_fails_on_kelly(self):
        assert _g53(_coin_book(wins=300))["status"] == "fail"  # mean exactly 0, Kelly 0
        assert _g53(_coin_book(wins=301))["status"] == "fail"  # Kelly 0.33%, half 0.17% < 1%

    def test_a_book_with_no_losing_trade_is_not_evaluated_not_a_crash(self):
        wins_only = [dataclasses.replace(t, r_net=0.5) for t in _coin_book(wins=600)]
        row = _g53(wins_only)
        assert row["status"] == "not_evaluated" and "unbounded" in row["evidence"]
        assert "intentional" in row["evidence"] and "not read that as a pass" in row["evidence"]

    def test_the_no_loss_limitation_is_stated_in_the_criteria_and_the_ledger(self):
        from pathlib import Path

        docs = Path(ev.__file__).parents[1] / "docs"
        criteria = (docs / "STRATEGY-ACCEPTANCE-CRITERIA.md").read_text(encoding="utf-8")
        ledger = (docs / "BACKTEST-ENGINE-READINESS.md").read_text(encoding="utf-8")
        row = next(ln for ln in criteria.splitlines() if ln.startswith("| G5.3 |"))
        assert "no losing trade" in row and "intentional, not a pass" in row
        line = next(ln for ln in ledger.splitlines() if ln.startswith("| G5.3 Sizing |"))
        assert "no losing trade" in line and "never a pass" in line

    def test_a_missing_or_invalid_recorded_risk_is_not_evaluated(self):
        book = _coin_book(wins=390)
        assert _g53(book, profile=None)["status"] == "not_evaluated"
        for bad in (0.0, 1.5, -0.01):
            assert _g53(book, risk=bad)["status"] == "not_evaluated"

    def test_too_few_trades_is_not_evaluated(self):
        row = _g53(_coin_book(wins=60, n=100))
        assert row["status"] == "not_evaluated" and "too few" in row["evidence"]

    def test_trades_without_a_defined_r_are_not_evaluated(self):
        book = _coin_book(wins=390)
        book[0] = dataclasses.replace(book[0], r_net=float("nan"))
        assert _g53(book)["status"] == "not_evaluated"

    def test_the_evidence_states_what_the_estimate_is_and_is_not(self):
        text = _g53(_coin_book(wins=390))["evidence"]
        assert "point estimate" in text and "uncapped trade list" in text
        assert "not proof that live sizing is safe" in text
        assert "not a re-derivation of live sizing" in text and "IST entry date" in text

    def test_it_is_registered_and_a_failed_row_blocks_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert "G5.3" in ev._EVALUATORS
        for status in ("fail", "not_evaluated"):
            result = _derive(_evaluation("pass", G5_3=status))
            assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G5.3"
