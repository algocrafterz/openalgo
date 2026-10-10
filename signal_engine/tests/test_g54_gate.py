"""G5.4 capacity: entry order size against the median same-time 5-minute volume."""

from __future__ import annotations

import copy
import dataclasses

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import capacity
from signal_engine.backtest import evaluation as ev
from signal_engine.backtest.live_caps import trade_hash
from signal_engine.tests.cost_fixtures import make_model
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row

IST = "Asia/Kolkata"


def _frame(days=30, volume=10_000.0, per_day=None, symbol_tod="09:20"):
    """Five-minute bars for `days` sessions; the 09:20 bar's volume per session can be set."""
    parts = []
    for k, day in enumerate(pd.bdate_range("2026-01-05", periods=days)):
        idx = pd.date_range(f"{day.date()} 09:15", periods=75, freq="5min", tz=IST)
        vol = np.full(75, volume)
        if per_day is not None:
            vol[1] = per_day[k]
        parts.append(pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": vol}, index=idx))
    return pd.concat(parts)


class TestMedianTable:
    def test_it_uses_only_previous_sessions_at_the_same_time_of_day(self):
        per_day = [100.0] * 25 + [1e9] * 5  # a spike late in the data must not leak backwards
        table = capacity.prior_median_volume(_frame(per_day=per_day))
        day = list(table.index)
        tod = 9 * 60 + 20
        assert table.at[day[24], tod] == 100.0
        assert table.at[day[26], tod] == 100.0  # one earlier spike does not move a median of 20
        assert np.isnan(table.at[day[0], tod])  # no history on the first session

    def test_the_entry_day_itself_is_never_in_its_own_median(self):
        per_day = [100.0] * 20 + [1e9]
        table = capacity.prior_median_volume(_frame(days=21, per_day=per_day))
        assert table.at[list(table.index)[20], 9 * 60 + 20] == 100.0

    def test_fewer_than_the_minimum_history_is_unmeasured(self):
        table = capacity.prior_median_volume(_frame(days=capacity.MIN_HISTORY))
        assert table.iloc[-1].isna().all()
        table = capacity.prior_median_volume(_frame(days=capacity.MIN_HISTORY + 1))
        assert table.iloc[-1].notna().all()


def _trade(day, hhmm="09:20", entry=100.0, symbol="A.NS"):
    base = _noisy_book(n=1)[0]
    ts = pd.Timestamp(f"{day} {hhmm}", tz=IST)
    return dataclasses.replace(base, symbol=symbol, entry_time=ts, exit_time=ts + pd.Timedelta(minutes=30),
                               entry=entry, signal_price=entry, sl=entry - 1.0, risk=1.0)


MODEL = make_model()


class TestMedianExactness:
    def test_it_equals_a_hand_computed_median_of_the_previous_twenty_same_time_bars(self):
        rng = np.random.default_rng(3)
        per_day = rng.integers(100, 10_000, size=40).astype(float)
        frame = _frame(days=40, volume=777.0, per_day=per_day)
        table = capacity.prior_median_volume(frame)
        days = list(table.index)
        for k in (10, 15, 25, 39):
            window = per_day[max(0, k - 20):k]  # the sessions before k, never k itself
            assert table.at[days[k], 9 * 60 + 20] == pytest.approx(float(np.median(window)))

    def test_other_times_of_day_do_not_leak_into_the_median(self):
        frame = _frame(days=30, volume=500.0)
        spike = frame.index.hour * 60 + frame.index.minute == 9 * 60 + 25
        frame.loc[spike, "Volume"] = 1e9  # every 09:25 bar is huge; 09:20 must not see it
        table = capacity.prior_median_volume(frame)
        assert table.at[list(table.index)[25], 9 * 60 + 20] == 500.0
        assert table.at[list(table.index)[25], 9 * 60 + 25] == 1e9

    def test_a_missing_bar_on_some_sessions_still_measures_when_enough_remain(self):
        frame = _frame(days=30, volume=500.0)
        drop = (frame.index.hour * 60 + frame.index.minute == 9 * 60 + 20) & (np.arange(len(frame)) // 75 % 2 == 0)
        table = capacity.prior_median_volume(frame[~drop])
        assert table.at[list(table.index)[29], 9 * 60 + 20] == 500.0  # 10 of the prior 20 present
        assert np.isnan(table.at[list(table.index)[5], 9 * 60 + 20])  # too few present yet


class TestParticipation:
    def test_shares_over_median_volume(self):
        frame = _frame(days=30, volume=1_000.0)
        medians = capacity.prior_median_volume(frame)
        day = pd.bdate_range("2026-01-05", periods=30)[25].date()
        t = _trade(day, entry=100.0)
        row = capacity.participation([t], MODEL, medians)[0]
        assert row["median_volume"] == 1_000.0
        assert row["ratio"] == pytest.approx(MODEL.notional(t) / 100.0 / 1_000.0)

    def test_a_zero_median_is_infinite_participation_not_a_pass(self):
        medians = capacity.prior_median_volume(_frame(days=30, volume=0.0))
        day = pd.bdate_range("2026-01-05", periods=30)[25].date()
        assert np.isinf(capacity.participation([_trade(day)], MODEL, medians)[0]["ratio"])

    def test_a_trade_with_no_history_is_unmeasured_nan(self):
        medians = capacity.prior_median_volume(_frame(days=30))
        day = pd.bdate_range("2026-01-05", periods=30)[2].date()
        assert np.isnan(capacity.participation([_trade(day)], MODEL, medians)[0]["ratio"])


class TestSizingFollowsTheCostModel:
    def test_the_cap_on_leverage_limits_the_shares(self):
        medians = capacity.prior_median_volume(_frame(days=30, volume=1_000.0))
        day = pd.bdate_range("2026-01-05", periods=30)[25].date()
        tight = dataclasses.replace(_trade(day, entry=100.0), risk=0.01, sl=99.99)  # huge risk-sized order
        row = capacity.participation([tight], MODEL, medians)[0]
        assert row["shares"] == pytest.approx(MODEL.capital * MODEL.leverage / 100.0)

    def test_a_risk_sized_order_uses_capital_times_risk_over_the_stop(self):
        medians = capacity.prior_median_volume(_frame(days=30, volume=1_000.0))
        day = pd.bdate_range("2026-01-05", periods=30)[25].date()
        wide = dataclasses.replace(_trade(day, entry=100.0), risk=5.0, sl=95.0)  # 5% stop
        row = capacity.participation([wide], MODEL, medians)[0]
        assert row["shares"] == pytest.approx(MODEL.capital * MODEL.risk_per_trade / 0.05 / 100.0)

    def test_participation_scales_with_capital_so_the_ceiling_does_not(self):
        base, trades = _stage(volume=5e4)
        double_model = dataclasses.replace(MODEL, capital=MODEL.capital * 2)
        frame = _frame(days=300, volume=5e4)
        doubled = capacity.run_stage(load_frames=lambda c: {"A.NS": frame.copy()}, symbols=["A.NS"],
                                     cost_model=double_model, headline_trades=trades)
        assert doubled["max_participation"] == pytest.approx(2 * base["max_participation"], rel=1e-12)
        assert doubled["capital_ceiling"] == pytest.approx(base["capital_ceiling"], rel=1e-12)
        at, at2 = base["capital_at_percentile"], doubled["capital_at_percentile"]
        assert all(at2[k] == pytest.approx(at[k], rel=1e-12) for k in at)


def _stage(volume, n=240, **kw):
    days = list(pd.bdate_range("2026-01-05", periods=300))[20:20 + n]
    trades = [_trade(d.date()) for d in days]
    frame = _frame(days=300, volume=volume)
    out = capacity.run_stage(load_frames=lambda chunk: {"A.NS": frame.copy()}, symbols=["A.NS"],
                             cost_model=MODEL, headline_trades=trades, shared={"bar_size": "5m"}, **kw)
    return out, trades


class TestStage:
    def test_a_liquid_book_is_complete_with_a_capital_ceiling(self):
        out, trades = _stage(volume=1e8)
        assert out["state"] == "complete" and out["over_cap"] == 0 and out["measured"] == len(trades)
        assert out["trade_sha256"] == trade_hash(trades) and out["diagnostic_not_trials"] is True
        assert out["capital_ceiling"] == pytest.approx(MODEL.capital * 0.10 / out["max_participation"])
        assert out["share_over"] == {"0.01": 0, "0.05": 0, "0.1": 0}
        at = out["capital_at_percentile"]
        assert set(at) == {"p50", "p95", "p99"} and at["p50"] >= at["p95"] >= at["p99"] >= out["capital_ceiling"]
        assert at["p95"] == pytest.approx(MODEL.capital * 0.10 / out["participation"]["p95"])

    def test_an_illiquid_book_counts_every_order_over_the_cap(self):
        out, trades = _stage(volume=10.0)
        assert out["over_cap"] == len(trades) and out["worst_orders"][0]["participation"] > 0.10

    def test_a_failing_load_is_an_error_state(self):
        def boom(chunk):
            raise RuntimeError("no bars")

        trades = [_trade(pd.bdate_range("2026-01-05", periods=40)[30].date())]
        out = capacity.run_stage(load_frames=boom, symbols=["A.NS"], cost_model=MODEL, headline_trades=trades)
        assert out["state"] == "error" and "no bars" in out["reason"]

    def test_it_is_deterministic(self):
        assert _stage(volume=5e4)[0] == _stage(volume=5e4)[0]


def _g54(trades, evidence):
    return _row(_evaluate(trades, capacity=evidence), "G5.4")


class TestGate:
    @pytest.fixture(scope="class")
    def liquid(self):
        out, trades = _stage(volume=1e8)
        return trades, out

    def test_every_order_within_the_cap_passes(self, liquid):
        trades, out = liquid
        row = _g54(trades, out)
        assert row["status"] == "pass" and row["hard"] is False
        assert "0 of 240 measured entry orders exceed 10%" in row["evidence"]
        assert "capital at which the largest order reaches the cap" in row["evidence"]
        assert "p50 order" in row["evidence"] and "p95 order" in row["evidence"]
        assert "not market impact" in row["evidence"]
        assert "does not prove an order would fill" in row["evidence"]

    def test_one_order_over_the_cap_fails(self, liquid):
        trades, out = liquid
        bad = {**out, "over_cap": 1}
        assert _g54(trades, bad)["status"] == "fail"

    def test_the_cap_boundary_in_volume_terms(self):
        shares = MODEL.notional(_trade(pd.bdate_range("2026-01-05", periods=40)[30].date())) / 100.0
        at_cap = shares / capacity.CAP  # the volume at which the order is exactly 10%
        inside, _ = _stage(volume=at_cap * 1.001)
        over, _ = _stage(volume=at_cap * 0.999)
        assert inside["over_cap"] == 0 and over["over_cap"] == over["measured"] > 0

    def test_one_real_order_over_the_cap_fails_the_gate_end_to_end(self):
        days = list(pd.bdate_range("2026-01-05", periods=300))[30:270]
        trades = [_trade(d.date()) for d in days]
        trades[100] = dataclasses.replace(trades[100], symbol="B.NS")
        liquid, thin = _frame(days=300, volume=1e8), _frame(days=300, volume=10.0)
        out = capacity.run_stage(load_frames=lambda chunk: {s: (liquid if s == "A.NS" else thin).copy() for s in chunk},
                                 symbols=["A.NS", "B.NS"], cost_model=MODEL, headline_trades=trades,
                                 shared={"bar_size": "5m"})
        assert out["over_cap"] == 1 and out["measured"] == 240
        row = _g54(trades, out)
        assert row["status"] == "fail" and "1 of 240 measured entry orders exceed 10%" in row["evidence"]

    @pytest.mark.parametrize("unmeasured,status", [(12, "pass"), (13, "not_evaluated")])
    def test_the_ninety_five_percent_coverage_boundary_is_inclusive(self, unmeasured, status):
        # the first sessions of the data have no history: their orders are unmeasured
        days = list(pd.bdate_range("2026-01-05", periods=300))
        early = [_trade(days[i % 10].date()) for i in range(unmeasured)]  # sessions 0 to 9 have no history
        later = [_trade(d.date()) for d in days[40:40 + 240 - unmeasured]]
        trades = early + later
        frame = _frame(days=300, volume=1e8)
        out = capacity.run_stage(load_frames=lambda chunk: {"A.NS": frame.copy()}, symbols=["A.NS"],
                                 cost_model=MODEL, headline_trades=trades, shared={"bar_size": "5m"})
        assert out["unmeasured"] == unmeasured and out["measured"] == 240 - unmeasured
        assert _g54(trades, out)["status"] == status

    def test_the_coverage_threshold_is_explicit_in_the_criteria_and_the_ledger(self):
        from pathlib import Path

        docs = Path(ev.__file__).parents[1] / "docs"
        criteria = (docs / "STRATEGY-ACCEPTANCE-CRITERIA.md").read_text(encoding="utf-8")
        ledger = (docs / "BACKTEST-ENGINE-READINESS.md").read_text(encoding="utf-8")
        row = next(ln for ln in criteria.splitlines() if ln.startswith("| G5.4 |"))
        line = next(ln for ln in ledger.splitlines() if ln.startswith("| G5.4 Capacity |"))
        pct = f"{ev.CAPACITY_MIN_MEASURED_SHARE:.0%}"
        assert pct in row and pct in line and "previous 20 sessions" in row and "excluding the entry day" in row

    def test_a_missing_stage_is_not_evaluated(self):
        assert _row(_evaluate(_noisy_book()), "G5.4")["status"] == "not_evaluated"

    def test_evidence_for_another_trade_list_is_not_evaluated(self, liquid):
        _, out = liquid
        row = _g54(_noisy_book(seed=9), out)
        assert row["status"] == "not_evaluated" and "different trade list" in row["evidence"]

    def test_too_few_or_mostly_unmeasured_orders_are_not_evaluated(self, liquid):
        trades, out = liquid
        few = {**out, "measured": 100}
        mostly = {**out, "measured": 200}  # 200 of 240 = 83% measured, below 95%
        for ev_ in (few, mostly):
            row = _g54(trades, ev_)
            assert row["status"] == "not_evaluated" and "never read as within the cap" in row["evidence"]

    def test_incomplete_or_invalid_stages_are_not_evaluated(self, liquid):
        trades, out = liquid
        assert _g54(trades, {**out, "state": "error", "reason": "x"})["status"] == "not_evaluated"
        assert _g54(trades, {**out, "state": "unsupported", "reason": "x"})["status"] == "not_evaluated"
        bad = copy.deepcopy(out)
        bad["diagnostic_not_trials"] = False
        assert _g54(trades, bad)["status"] == "not_evaluated"
        assert _g54(trades, "text")["status"] == "not_evaluated"

    def test_a_different_cap_is_not_the_criterion(self, liquid):
        trades, out = liquid
        assert _g54(trades, {**out, "cap": 0.5})["status"] == "not_evaluated"

    def test_a_zero_volume_order_has_no_finite_peak_and_fails(self):
        out, trades = _stage(volume=0.0)
        row = _g54(trades, out)
        assert row["status"] == "fail" and "no measurable volume" in row["evidence"]

    def test_it_is_registered_and_a_not_evaluated_row_blocks_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert "G5.4" in ev._EVALUATORS
        result = _derive(_evaluation("pass", G5_4="not_evaluated"))
        assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G5.4"


def test_the_record_flag_and_stage_hooks_exist():
    import inspect

    from signal_engine.backtest import record_run as rr

    assert "run_capacity" in inspect.signature(rr.record_idea).parameters
    assert "--capacity" in inspect.getsource(rr.main)
    assert rr._capacity_stage(supported=False, trades=[])["state"] == "unsupported"
