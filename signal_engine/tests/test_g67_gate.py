"""G6.7 evaluator: the fee schedule's recorded `verified_on` must be within a quarter of the run date."""

from __future__ import annotations

from signal_engine.backtest import evaluation as ev
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row


def _profile(verified_on="2026-10-01", assumed=False, **extra):
    broker = {"key": "flattrade", "label": "Flattrade", "source": "https://flattrade.in/charges/",
              "verified_on": verified_on, "assumed": assumed}  # fmt: skip
    broker.update(extra)
    return {"broker": broker, "mean_total_bps": 10.0, "flat_bps": 16.0, "product": "MIS",
            "exchange": "NSE", "capital": 1e5, "risk_per_trade": 0.01, "leverage": 5.0,
            "slippage_bps_per_side": 4.0, "total_bps_at": {"1.0": 10.0, "1.5": 12.0, "2.0": 14.0},
            "net_bps": 5.0, "net_bps_slip_2x": 3.0}  # fmt: skip


def _g67(run_date="2026-10-09", profile=None, **kw):
    kw.setdefault("cost_profile", _profile() if profile is None else profile)
    return _row(_evaluate(_noisy_book(), run_date=run_date, **kw), "G6.7")


class TestG67ChargesCurrent:
    def test_a_recent_verification_passes(self):
        row = _g67()
        assert row["status"] == "pass" and row["value"] == 8.0
        assert "flattrade" in row["evidence"] and "not re-checked against the broker" in row["evidence"]

    def test_the_quarter_boundary_is_inclusive(self):
        assert _g67(run_date="2026-10-01", profile=_profile("2026-07-01"))["status"] == "pass"  # 92 days
        assert _g67(run_date="2026-10-02", profile=_profile("2026-07-01"))["status"] == "fail"  # 93 days

    def test_a_stale_schedule_fails_and_says_so(self):
        row = _g67(profile=_profile("2025-01-01"))
        assert row["status"] == "fail" and row["evidence"].startswith("stale")

    def test_an_assumed_default_schedule_fails(self):
        row = _g67(profile=_profile(assumed=True))
        assert row["status"] == "fail" and "no verified fee schedule" in row["evidence"]

    def test_no_run_date_is_not_evaluated_and_never_reads_the_clock(self):
        row = _row(_evaluate(_noisy_book(), cost_profile=_profile()), "G6.7")
        assert row["status"] == "not_evaluated" and "no run date" in row["evidence"]

    def test_no_cost_profile_is_not_evaluated(self):
        row = _row(_evaluate(_noisy_book(), run_date="2026-10-09"), "G6.7")
        assert row["status"] == "not_evaluated"

    def test_a_missing_or_unparseable_date_is_not_evaluated(self):
        assert _g67(profile=_profile(None))["status"] == "not_evaluated"
        assert _g67(profile=_profile("soon"))["status"] == "not_evaluated"
        assert _g67(run_date="later")["status"] == "not_evaluated"

    def test_a_schedule_verified_after_the_run_date_is_not_evaluated(self):
        assert _g67(run_date="2026-09-01")["status"] == "not_evaluated"

    def test_it_is_registered_and_not_hard(self):
        assert "G6.7" in ev._EVALUATORS
        assert _g67()["hard"] is False


class TestRunDatePropagation:
    def test_the_recorded_run_ages_the_schedule_from_its_own_run_date(self):
        from signal_engine.tests.test_statutory_headline import _book, _record, _row as record_row
        from signal_engine.tests.test_statutory_headline import make_model

        record, _ = _record(_book(), make_model())
        row = record_row(record, "G6.7")
        assert row["status"] == "pass" and f"before the run date {record['date']}" in row["evidence"]

    def test_evaluate_without_a_run_date_still_works(self):
        assert _row(_evaluate(_noisy_book()), "G6.7")["status"] == "not_evaluated"


class TestCriteriaAlignment:
    def test_the_acceptance_criteria_state_the_same_window(self):
        from pathlib import Path

        text = (Path(ev.__file__).parents[1] / "docs" / "STRATEGY-ACCEPTANCE-CRITERIA.md").read_text(
            encoding="utf-8"
        )
        line = next(ln for ln in text.splitlines() if ln.startswith("| G6.7 |"))
        assert f"{ev.CHARGES_MAX_AGE_DAYS} days" in line and "including the run date" in line
        assert str(ev.CHARGES_MAX_AGE_DAYS) in _g67()["threshold"]
