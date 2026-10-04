"""Tearsheet writer: turns a trade list into a schema-v2 run record.

The conventions pinned here are the ones that decide whether two runs can be compared:
costs are deducted in the headline, degenerate statistics are omitted (never reported as
0), drawdown follows calendar order, and every record validates against the registry.
"""

import json
import math
from datetime import date

import pandas as pd
import pytest

from signal_engine.backtest.tearsheet import MAX_SERIES_POINTS, build_run_record, downsample
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br

META = {
    "run_id": "2026-09-30-orb-v1",
    "run_date": "2026-09-30",
    "code_version": "abc1234",
    "data": {"source": "historify", "as_of": "2026-09-29", "universe": "fno-208", "bar_size": "5m"},
    "period": {"start": "2026-01-01", "end": "2026-06-30"},
    "costs": {"model": "zerodha-mis", "round_trip_bps": 9.4},
}


def _trade(day, r_gross, r_net, sym="A.NS", risk_pct=1.0):
    price = 100.0
    return Trade(
        symbol=sym,
        day=day,
        direction=1,
        tag="t",
        entry_time=None,
        entry=price,
        signal_price=price,
        sl=price * (1 - risk_pct / 100),
        tp=0.0,
        risk=price * risk_pct / 100,
        exit_time=f"{day} 15:00",
        reason="tp",
        r_gross=r_gross,
        r_net=r_net,
    )


def _trades(n=40):
    out = []
    for i in range(n):
        d = date(2026, 1 + i // 10, 1 + i % 10)
        win = i % 3 != 0
        out.append(_trade(d, 1.1 if win else -1.0, 1.0 if win else -1.1))
    return out


def test_headline_metrics_are_costs_included_and_bps_based():
    rec = build_run_record(_trades(), **META)
    m = rec["metrics"]
    assert m["trades"] == 40
    # risk_pct 1.0 -> 1R == 100 bps of notional
    assert m["net_bps"] < m["gross_bps"]
    assert m["gross_bps"] - m["net_bps"] == pytest.approx(10.0, abs=1e-6)  # 0.1R cost/trade
    assert 0 < m["win_rate"] < 1
    assert m["profit_factor"] > 0
    assert m["hurdle_t"] > 1.9


def test_record_validates_against_registry_schema():
    rec = build_run_record(_trades(), **META, params={"x": 1}, n_trials=4, change_note="baseline")
    assert br._validate_run(json.loads(json.dumps(rec, allow_nan=False))) == []


def test_record_is_strict_json_no_nan():
    rec = build_run_record(_trades(), **META)
    json.dumps(rec, allow_nan=False)  # raises on NaN / inf


def test_empty_trade_list_gives_valid_record_with_zero_trades():
    rec = build_run_record([], **META)
    assert rec["metrics"]["trades"] == 0
    assert "sharpe" not in rec["metrics"]  # unknown, not zero
    assert br._validate_run(rec) == []
    assert rec["series"]["equity_r"] == []


def test_degenerate_statistics_are_omitted_not_zero():
    rec = build_run_record([_trade(date(2026, 1, 2), 1.0, 0.9)], **META)
    for key in ("sharpe", "t_stat"):
        assert key not in rec["metrics"]


def test_drawdown_follows_calendar_order_not_list_order():
    # Same trades, shuffled input order, must give the same equity curve.
    ts = _trades()
    a = build_run_record(ts, **META)["series"]["equity_r"]
    b = build_run_record(list(reversed(ts)), **META)["series"]["equity_r"]
    assert a == b


def test_equity_is_cumulative_net_r_one_point_per_day_and_drawdown_nonpositive():
    rec = build_run_record(_trades(), **META)
    eq = rec["series"]["equity_r"]
    assert eq[-1][1] == pytest.approx(sum(t.r_net for t in _trades()))
    assert len({p[0] for p in eq}) == len(eq)
    assert all(p[1] <= 0 for p in rec["series"]["drawdown_r"])
    assert rec["metrics"]["max_dd_r"] == pytest.approx(
        -min(p[1] for p in rec["series"]["drawdown_r"])
    )


def test_monthly_breakdown_sums_to_total():
    rec = build_run_record(_trades(), **META)
    assert sum(m["n"] for m in rec["series"]["monthly"]) == 40
    assert sum(m["total_r"] for m in rec["series"]["monthly"]) == pytest.approx(
        rec["series"]["equity_r"][-1][1], abs=0.05
    )


def test_cost_sensitivity_and_breakeven():
    rec = build_run_record(_trades(), **META)
    cs = {p["round_trip_bps"]: p["net_bps"] for p in rec["cost_sensitivity"]}
    assert cs[0] == pytest.approx(rec["metrics"]["gross_bps"])
    assert cs[20] == pytest.approx(rec["metrics"]["gross_bps"] - 20)
    assert rec["metrics"]["breakeven_cost_bps"] == pytest.approx(rec["metrics"]["gross_bps"])


def test_oos_split_reports_both_halves():
    rec = build_run_record(_trades(), **META, oos_start="2026-03-01")
    assert rec["split"] == {"type": "oos", "oos_start": "2026-03-01"}
    assert rec["in_sample"]["trades"] + rec["out_of_sample"]["trades"] == 40
    assert rec["out_of_sample"]["trades"] > 0


def test_oos_start_outside_range_is_rejected():
    with pytest.raises(ValueError, match="oos_start"):
        build_run_record(_trades(), **META, oos_start="2030-01-01")


def test_lineage_fields_are_passed_through():
    rec = build_run_record(
        _trades(), **META, parent_run_id="2026-09-01-orb-v0", change_note="tighter SL", n_trials=7
    )
    assert rec["parent_run_id"] == "2026-09-01-orb-v0"
    assert rec["change_note"] == "tighter SL"
    assert rec["n_trials"] == 7
    assert rec["promoted"] is False


def test_n_trials_below_one_is_rejected():
    with pytest.raises(ValueError, match="n_trials"):
        build_run_record(_trades(), **META, n_trials=0)


class TestDownsample:
    def test_short_series_untouched(self):
        pts = [[str(i), float(i)] for i in range(10)]
        assert downsample(pts, 500) == pts

    def test_long_series_capped_and_keeps_endpoints(self):
        pts = [[str(i), float(i)] for i in range(5000)]
        out = downsample(pts, MAX_SERIES_POINTS)
        assert len(out) <= MAX_SERIES_POINTS
        assert out[0] == pts[0] and out[-1] == pts[-1]

    def test_downsample_preserves_the_deepest_drawdown(self):
        pts = [[str(i), 0.0] for i in range(5000)]
        pts[1234] = ["1234", -9.0]
        out = downsample(pts, 100, keep="min")
        assert min(p[1] for p in out) == -9.0


class TestPositionCaps:
    """The record carries the concurrency profile and what is left at N slots."""

    @staticmethod
    def _overlapping(n=6):
        # All trades open on consecutive days and close together, so a cap must bind.
        out = []
        for i in range(n):
            t = _trade(date(2026, 1, 1 + i), 1.0, 0.9, sym=f"S{i}.NS")
            t.entry_time = pd.Timestamp(f"2026-01-{1 + i:02d}")
            t.exit_time = pd.Timestamp("2026-02-01")
            out.append(t)
        return out

    def test_default_record_has_no_cap_data(self):
        rec = build_run_record(_trades(), **META)
        assert "position_caps" not in rec
        assert "peak_concurrent" not in rec["metrics"]

    def test_profile_and_caps_are_recorded(self):
        rec = build_run_record(self._overlapping(), **META, position_caps=(2, 4))
        assert rec["metrics"]["peak_concurrent"] == 6
        assert rec["metrics"]["median_concurrent"] == 3.5
        caps = {c["max_positions"]: c for c in rec["position_caps"]}
        assert set(caps) == {2, 4}
        assert caps[2]["trades"] == 2 and caps[4]["trades"] == 4
        assert caps[2]["taken_pct"] == pytest.approx(100 * 2 / 6, abs=0.1)

    def test_cap_at_or_above_peak_matches_the_headline(self):
        rec = build_run_record(self._overlapping(), **META, position_caps=(6, 50))
        for cap in rec["position_caps"]:
            assert cap["trades"] == 6
            assert cap["net_bps"] == pytest.approx(rec["metrics"]["net_bps"])
            assert cap["taken_pct"] == 100.0

    def test_capped_record_validates_and_is_strict_json(self):
        rec = build_run_record(self._overlapping(), **META, position_caps=(3, 10))
        assert br._validate_run(json.loads(json.dumps(rec, allow_nan=False))) == []

    def test_caps_are_reported_in_ascending_order(self):
        rec = build_run_record(self._overlapping(), **META, position_caps=(10, 3, 5))
        assert [c["max_positions"] for c in rec["position_caps"]] == [3, 5, 10]

    def test_cap_below_one_is_rejected(self):
        with pytest.raises(ValueError, match="max_positions"):
            build_run_record(self._overlapping(), **META, position_caps=(0,))

    def test_caps_need_entry_times(self):
        with pytest.raises(ValueError, match="entry_time"):
            build_run_record(_trades(), **META, position_caps=(5,))

    def test_empty_trades_with_caps_gives_valid_record(self):
        rec = build_run_record([], **META, position_caps=(5,))
        assert br._validate_run(rec) == []
        assert rec["position_caps"][0]["trades"] == 0


class TestScoringMatchesTheHeadlineUnit:
    """t_stat and sharpe are in bps, like net_bps.

    R weights every trade equally whatever its stop width; bps weights by notional. A run
    can have a negative mean R and a positive mean bps, and scoring one in R and the other
    in bps put a positive headline next to a negative t. With one trade per day (as here)
    the two now agree. With uneven trades per day they can still differ, because t counts
    each day once; see the heavy-day test below.
    """

    @staticmethod
    def _mixed():
        out = []
        for i in range(20):
            day = date(2026, 1, 1 + i)
            if i % 2 == 0:  # tiny stop, loses 1R = 10 bps
                out.append(_trade(day, -0.9, -1.0, sym=f"A{i}.NS", risk_pct=0.1))
            else:  # wide stop, wins 0.5R = 250 bps
                out.append(_trade(day, 0.6, 0.5, sym=f"B{i}.NS", risk_pct=5.0))
        return out

    def test_headline_and_scores_agree_when_mean_r_and_mean_bps_disagree(self):
        rec = build_run_record(self._mixed(), **META)
        m = rec["metrics"]
        assert sum(t.r_net for t in self._mixed()) < 0  # mean R is negative
        assert m["net_bps"] == pytest.approx(120.0)  # mean bps is positive
        assert m["t_stat"] > 0
        assert m["sharpe"] > 0

    def test_t_stat_is_the_day_clustered_t_of_net_bps(self):
        import numpy as np

        trades = self._mixed()
        bps = np.array([t.r_net * t.risk_pct * 100 for t in trades])
        expected = bps.mean() / (bps.std(ddof=1) / np.sqrt(len(bps)))  # one trade per day
        rec = build_run_record(trades, **META)
        assert rec["metrics"]["t_stat"] == pytest.approx(expected, abs=1e-3)

    def test_sharpe_is_annualised_from_daily_bps(self):
        import numpy as np

        trades = self._mixed()
        daily = np.array([t.r_net * t.risk_pct * 100 for t in trades])
        expected = daily.mean() / daily.std(ddof=1) * np.sqrt(252)
        rec = build_run_record(trades, **META)
        assert rec["metrics"]["sharpe"] == pytest.approx(expected, abs=1e-3)

    def test_out_of_sample_half_uses_the_same_unit(self):
        rec = build_run_record(self._mixed(), **META, oos_start="2026-01-11")
        assert rec["out_of_sample"]["net_bps"] > 0 and rec["out_of_sample"]["t_stat"] > 0


class TestBaselinePassThrough:
    BASELINE = {
        "method": "same-symbol random entry",
        "draws_per_trade": 20,
        "trades": 40,
        "skipped": 0,
        "gross_bps": 30.0,
        "net_bps": 6.0,
        "excess_net_bps": 20.5,
        "excess_t_stat": 1.4,
    }

    def test_absent_by_default(self):
        assert "baseline" not in build_run_record(_trades(), **META)

    def test_baseline_is_stored_as_a_copy_and_validates(self):
        given = dict(self.BASELINE)
        rec = build_run_record(_trades(), **META, baseline=given)
        assert rec["baseline"] == self.BASELINE
        assert rec["baseline"] is not given
        assert br._validate_run(json.loads(json.dumps(rec, allow_nan=False))) == []

    def test_heavy_signal_days_can_still_flip_the_sign_of_t(self):
        """Per-trade mean is positive, but each day counts once and the quiet days lose.

        Documents the behaviour so it is not mistaken for a bug: day 1 has nine winners,
        the next nine days have one small loser each.
        """
        trades = [
            _trade(date(2026, 1, 1), 0.6, 0.5, sym=f"W{i}.NS", risk_pct=1.0) for i in range(9)
        ]
        trades += [
            _trade(date(2026, 1, 2 + i), -0.1, -0.2, sym=f"L{i}.NS", risk_pct=1.0) for i in range(9)
        ]
        m = build_run_record(trades, **META)["metrics"]
        assert m["net_bps"] > 0  # (9 * 50 - 9 * 20) / 18 = +15 bps per trade
        assert m["t_stat"] < 0  # daily means: +50, then nine days of -20


def test_headline_metrics_carry_total_cost_in_r():
    rec = build_run_record(_trades(), **META)
    trades = _trades()
    expected = sum(t.r_gross - t.r_net for t in trades)
    assert rec["metrics"]["total_cost_r"] == pytest.approx(expected, abs=1e-3)
