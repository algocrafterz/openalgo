"""Gates that became computable: G1.1, G1.3, G2.3, G2.6, G2.7, G3.3, G3.5."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest.types import Trade


def _t(day, r_net, symbol="A.NS", exit_day=None):
    return Trade(
        symbol=symbol,
        day=day,
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=pd.Timestamp(f"{exit_day or day} 14:30"),
        reason="tp",
        r_gross=r_net + 0.1,
        r_net=r_net,
    )


def _no_stop(trade):
    """The same trade without a stop: R is undefined, the result is a return on notional."""
    return dataclasses.replace(
        trade,
        sl=float("nan"),
        risk=float("nan"),
        r_gross=float("nan"),
        r_net=float("nan"),
        gross_ret=(trade.r_net + 0.1) / 100,
        net_ret=trade.r_net / 100,
    )


def _noisy_book(n=600, mean=0.15, seed=0, start="2022-01-03"):
    rng = np.random.default_rng(seed)
    days = [d.date().isoformat() for d in pd.bdate_range(start, periods=n)]
    return [_t(d, float(rng.normal(mean, 1.0))) for d in days]


def _evaluate(trades, **kw):
    args = {
        "metrics": {"trades": len(trades), "t_stat": 3.0, "gross_bps": 60.0, "net_bps": 30.0},
        "n_trials": 1,
        "horizon": "intraday",
        "cost_bps": 16.0,
        "oos_start": None,
        "position_caps": None,
        "params": None,
    }
    args.update(kw)
    return ev.evaluate(trades, **args)


def _row(profile, gate):
    rows = [r for d in profile["dimensions"] for r in d["rows"] if r["gate"] == gate]
    assert len(rows) == 1
    return rows[0]


PROFILE = {
    "model": "statutory-per-trade",
    "product": "MIS",
    "exchange": "NSE",
    "capital": 100000.0,
    "risk_per_trade": 0.01,
    "leverage": 5.0,
    "slippage_bps_per_side": 4.0,
    "flat_bps": 16.0,
    "mean_statutory_bps": 7.0,
    "mean_total_bps": 15.0,
    "total_bps_at": {"1.0": 15.0, "1.5": 19.0, "2.0": 23.0},
    "net_r": 0.12,
    "net_r_slip_2x": 0.05,
    "trades": 600,
}


class TestCostGates:
    def test_g11_passes_with_a_statutory_model(self):
        row = _row(_evaluate(_noisy_book(), cost_profile=PROFILE), "G1.1")
        assert row["status"] == "pass"
        assert "statutory" in row["evidence"] and "100000" in row["evidence"].replace(",", "")

    def test_g11_unchanged_without_a_profile(self):
        row = _row(_evaluate(_noisy_book(), cost_model="flat-round-trip-bps"), "G1.1")
        assert row["status"] == "report"

    def test_g13_passes_when_net_survives_doubled_slippage(self):
        row = _row(_evaluate(_noisy_book(), cost_profile=PROFILE), "G1.3")
        assert row["status"] == "pass" and row["hard"] is True
        assert "1.5x" in row["evidence"] and "2.0x" in row["evidence"]

    def test_g13_fails_when_doubled_slippage_turns_net_negative(self):
        bad = {**PROFILE, "net_r_slip_2x": -0.02}
        assert _row(_evaluate(_noisy_book(), cost_profile=bad), "G1.3")["status"] == "fail"

    def test_g13_not_evaluated_without_a_profile(self):
        assert _row(_evaluate(_noisy_book()), "G1.3")["status"] == "not_evaluated"


class TestSharpeGates:
    def test_g23_passes_with_a_clear_edge(self):
        row = _row(_evaluate(_noisy_book(n=1500, mean=0.2)), "G2.3")
        assert row["status"] == "pass"
        assert row["value"] >= 0.90

    def test_g23_fails_without_edge(self):
        assert _row(_evaluate(_noisy_book(n=1500, mean=-0.05, seed=3)), "G2.3")["status"] == "fail"

    def test_g23_uses_the_trial_count(self):
        many = _row(_evaluate(_noisy_book(n=1500, mean=0.07, seed=5), n_trials=200), "G2.3")
        one = _row(_evaluate(_noisy_book(n=1500, mean=0.07, seed=5), n_trials=1), "G2.3")
        assert many["value"] < one["value"]

    def test_g23_states_its_variance_assumption(self):
        row = _row(_evaluate(_noisy_book(n=600)), "G2.3")
        assert "assum" in row["evidence"].lower()

    def test_g24_without_a_confirmed_ledger_stays_not_evaluated_and_says_why(self):
        row = _row(_evaluate(_noisy_book()), "G2.4")
        assert row["status"] == "not_evaluated"
        assert "ledger is not confirmed" in row["evidence"]

    def test_g24_with_several_confirmed_trials_stays_not_evaluated_and_says_why(self):
        ledger = {"source": "trials.yaml", "total": 3, "material": 3, "external": 0}
        row = _row(_evaluate(_noisy_book(), n_trials=3, trials=ledger), "G2.4")
        assert row["status"] == "not_evaluated" and "grid" in row["evidence"]

    def test_g26_reports_the_newey_west_t(self):
        row = _row(_evaluate(_noisy_book(n=800, mean=0.1)), "G2.6")
        assert row["status"] == "report"
        assert "Newey-West" in row["evidence"]
        assert isinstance(row["value"], float)

    def test_g27_reports_track_length_against_the_minimum(self):
        row = _row(_evaluate(_noisy_book(n=800, mean=0.1)), "G2.7")
        assert row["status"] == "report"
        assert "sessions" in row["evidence"]

    def test_g27_infinite_minimum_is_reported_not_crashed(self):
        row = _row(_evaluate(_noisy_book(n=800, mean=-0.2)), "G2.7")
        assert row["status"] == "report"
        assert row["value"] is None


class TestWalkForward:
    def test_passes_when_most_windows_are_positive(self):
        row = _row(_evaluate(_noisy_book(n=1000, mean=0.3, seed=1)), "G3.3")
        assert row["status"] == "pass"

    def test_fails_when_most_windows_lose(self):
        row = _row(_evaluate(_noisy_book(n=1000, mean=-0.3, seed=1)), "G3.3")
        assert row["status"] == "fail"

    def test_needs_four_full_windows(self):
        row = _row(_evaluate(_noisy_book(n=100)), "G3.3")
        assert row["status"] == "not_evaluated"
        assert "4" in row["evidence"]

    def test_trades_without_a_stop_are_scored_in_net_bps(self):
        # Regression: G3.3 read r_net, which is NaN without a stop, so every window scored
        # NaN and the gate reported "0 of N windows net positive" whatever the trades earned.
        book = [_no_stop(t) for t in _noisy_book(n=1000, mean=0.3, seed=1)]
        row = _row(_evaluate(book), "G3.3")
        assert row["status"] == "pass"
        assert not row["evidence"].startswith("0 of")

    def test_losing_trades_without_a_stop_still_fail(self):
        book = [_no_stop(t) for t in _noisy_book(n=1000, mean=-0.3, seed=1)]
        assert _row(_evaluate(book), "G3.3")["status"] == "fail"


class TestLeakage:
    def _swing_book(self):
        days = [d.date().isoformat() for d in pd.bdate_range("2024-01-01", periods=60)]
        return [_t(d, 0.1, exit_day=days[min(i + 3, 59)]) for i, d in enumerate(days)]

    def test_g35_fails_without_an_embargo(self):
        trades = self._swing_book()
        row = _row(_evaluate(trades, oos_start="2024-02-12"), "G3.5")
        assert row["status"] == "fail"
        assert "straddl" in row["evidence"]

    def test_g35_passes_with_purge_and_embargo_recorded(self):
        trades = self._swing_book()
        split = {
            "type": "oos",
            "oos_start": "2024-02-12",
            "embargo_sessions": 1,
            "purged_in_sample": 3,
            "embargoed_oos": 1,
        }
        row = _row(_evaluate(trades, oos_start="2024-02-12", split=split), "G3.5")
        assert row["status"] == "pass"

    def test_g35_not_evaluated_without_a_split(self):
        assert _row(_evaluate(self._swing_book()), "G3.5")["status"] == "not_evaluated"
