"""Exposure, concurrency, turnover, holding time and concentration (read-only report)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import exposure_turnover as et

T0 = pd.Timestamp("2024-01-01 04:00", tz="UTC")  # 09:30 IST
NOTIONAL = 100_000.0
ASSUMPTIONS = {
    "sizing.capital_per_trade": {"value": 100000, "id": "A-025", "source": "user"},
    "sizing.basis": {"value": "fixed_notional", "id": "A-026", "source": "user"},
    "portfolio.mode": {"value": "single_position", "id": "A-032", "source": "user"},
    "portfolio.scope": {"value": "per_symbol", "id": "A-033", "source": "user"},
}


def _t(symbol, side, a, b, reason="TP", gross=10.0, cost=12.0, day=0):
    start = T0 + pd.Timedelta(days=day)
    return {
        "symbol": symbol,
        "side": side,
        "entry_time": start + pd.Timedelta(minutes=5 * a),
        "exit_time": start + pd.Timedelta(minutes=5 * b),
        "exit_reason": reason,
        "gross_bps": gross,
        "cost_bps": cost,
        "net_bps": gross - cost,
    }


def _frame(rows):
    return pd.DataFrame(rows)


class TestOccupancy:
    def test_two_overlapping_positions(self):
        # A holds bars 0-2, B holds bars 1-3: concurrency 1,2,2,1 over four bars
        o = et.occupancy(_frame([_t("A", "BUY", 0, 2), _t("B", "SELL", 1, 3)]), NOTIONAL)
        q = o["concurrent_positions_time_weighted"]
        assert q["bars"] == 4 and q["max"] == 2 and q["mean"] == pytest.approx(1.5)
        assert o["peak"]["concurrent_positions"] == 2
        assert (o["peak"]["long"], o["peak"]["short"]) == (1, 1)
        assert o["gross_exposure_notional_time_weighted"]["max"] == 2 * NOTIONAL

    def test_net_exposure_nets_longs_against_shorts(self):
        t = _frame([_t("A", "BUY", 0, 1), _t("B", "SELL", 0, 1), _t("C", "SELL", 0, 1)])
        o = et.occupancy(t, NOTIONAL)
        assert o["net_exposure_notional_time_weighted"]["p50"] == -NOTIONAL
        assert o["share_of_active_bar_time_net_long"] == 0.0

    def test_back_to_back_positions_do_not_overlap(self):
        # A exits in bar 1, B enters in bar 2: the exit bar is occupied, the next is not
        t = _frame([_t("A", "BUY", 0, 1), _t("B", "BUY", 2, 3)])
        assert et.occupancy(t, NOTIONAL)["concurrent_positions_time_weighted"]["max"] == 1

    def test_same_bar_entry_and_exit_counts_one_bar(self):
        o = et.occupancy(_frame([_t("A", "BUY", 3, 3)]), NOTIONAL)
        assert o["concurrent_positions_time_weighted"]["bars"] == 1

    def test_gap_between_positions_is_not_counted_as_active_time(self):
        t = _frame([_t("A", "BUY", 0, 0), _t("B", "BUY", 10, 10)])
        assert et.occupancy(t, NOTIONAL)["concurrent_positions_time_weighted"]["bars"] == 2

    def test_overnight_position_is_counted(self):
        late = {**_t("B", "BUY", 0, 0), "exit_time": T0 + pd.Timedelta(days=1)}
        assert et.occupancy(_frame([_t("A", "BUY", 0, 0), late]), NOTIONAL)["overnight_positions"] == 1

    def test_peak_per_session_and_reference_profile_agree_on_the_peak(self):
        rows = [_t(f"S{i}", "BUY", 0, 4) for i in range(5)] + [_t("X", "BUY", 0, 0, day=1)]
        o = et.occupancy(_frame(rows), NOTIONAL)
        assert o["peak"]["concurrent_positions"] == 5 == o["reference_entry_profile"]["peak"]
        assert o["sessions_with_a_position"] == 2
        assert o["peak_concurrency_per_session"]["max"] == 5


class TestHoldingTurnoverConcentration:
    def test_holding_minutes_include_the_exit_bar(self):
        h = et.holding(_frame([_t("A", "BUY", 0, 0), _t("B", "BUY", 0, 5)]))
        assert h["all"]["mean"] == pytest.approx((5 + 30) / 2)
        assert h["share_within_one_bar"] == 0.5

    def test_holding_by_exit_reason_withholds_small_groups(self):
        h = et.holding(_frame([_t("A", "BUY", 0, 1, reason="SL")]))
        assert h["by_exit_reason"]["SL"]["withheld"] is True

    def test_turnover_counts_round_trips_and_notional(self):
        rows = [_t(f"S{i}", "BUY", 0, 1) for i in range(3)] + [_t("S0", "BUY", 0, 1, day=1)]
        tv = et.turnover(_frame(rows), NOTIONAL)
        assert tv["round_trips_total"] == 4
        assert tv["notional_traded_total"] == 4 * 2 * NOTIONAL
        assert tv["sessions_with_a_trade"] == 2
        assert tv["round_trips_per_session"]["max"] == 3
        assert tv["entries_in_busiest_bar_of_session"]["max"] == 3

    def test_entry_time_buckets_are_ist_and_sum_to_one(self):
        tv = et.turnover(_frame([_t("A", "BUY", 0, 1), _t("B", "BUY", 6, 7)]), NOTIONAL)
        b = tv["entry_time_share_by_30_min_ist"]
        assert b["09:30"] == 0.5 and b["10:00"] == 0.5
        assert sum(b.values()) == pytest.approx(1.0)

    def test_concentration_of_equal_symbols(self):
        c = et.concentration(_frame([_t(s, "BUY", 0, 1) for s in "ABCD" for _ in range(5)]))
        assert c["herfindahl"] == pytest.approx(0.25)
        assert c["effective_number_of_symbols"] == pytest.approx(4.0)
        assert c["top_5_symbol_trade_share"] == 1.0

    def test_concentration_of_one_dominant_symbol(self):
        c = et.concentration(_frame([_t("A", "BUY", 0, 1)] * 9 + [_t("B", "SELL", 0, 1)]))
        assert c["most_traded"][0] == {"symbol": "A", "share": 0.9}
        assert c["long_share_of_trades"] == 0.9

    def test_money_restates_bps_at_the_fixed_notional(self):
        m = et.money(_frame([_t("A", "BUY", 0, 1, gross=10, cost=12)] * 2), NOTIONAL)
        assert m["gross"] == pytest.approx(2 * 10 * 10.0)
        assert m["costs"] == pytest.approx(2 * 12 * 10.0)
        assert m["net"] == pytest.approx(-2 * 2 * 10.0)


class TestAssumptionsAndEvidence:
    def _run(self, tmp_path):
        run = tmp_path / "run"
        (run / "evidence").mkdir(parents=True)
        _frame([_t("A", "BUY", 0, 1), _t("B", "SELL", 0, 2)]).to_parquet(
            run / "evidence" / "trades.parquet"
        )
        entries = [
            {"field": k, "id": v["id"], "source": v["source"], "value": str(v["value"])}
            for k, v in ASSUMPTIONS.items()
        ]
        (run / "assumptions.json").write_text(json.dumps({"entries": entries}))
        return run

    def test_assumptions_are_read_with_their_ids(self, tmp_path):
        a = et.read_assumptions(self._run(tmp_path))
        assert a["sizing.capital_per_trade"]["id"] == "A-025"
        assert et.notional_from(a) == NOTIONAL

    def test_missing_or_invalid_notional_raises(self):
        for bad in ({}, {"sizing.capital_per_trade": {"value": "abc"}},
                    {"sizing.capital_per_trade": {"value": "-5"}}):
            with pytest.raises(et.ExposureError):
                et.notional_from(bad)

    def test_missing_columns_and_empty_raise(self):
        with pytest.raises(et.ExposureError):
            et.build_report(pd.DataFrame([{"symbol": "A"}]), ASSUMPTIONS)
        with pytest.raises(et.ExposureError):
            et.build_report(_frame([_t("A", "BUY", 0, 1)]).iloc[0:0], ASSUMPTIONS)

    def test_sealed_payload_is_valid_deterministic_strict_json_and_read_only(self, tmp_path):
        run = self._run(tmp_path)
        before = sorted(p.name for p in run.rglob("*"))
        ev1, ev2 = et.evidence_for_run(run), et.evidence_for_run(run)
        assert ev1 == ev2 and et.validate_evidence(ev1) == []
        assert ev1["report_only"] and ev1["diagnostic_not_trials"]
        assert ev1["report"]["assumptions"]["portfolio.scope"]["value"] == "per_symbol"
        json.dumps(ev1, allow_nan=False)
        assert sorted(p.name for p in run.rglob("*")) == before

    def test_tampering_breaks_the_seal(self, tmp_path):
        ev = et.evidence_for_run(self._run(tmp_path))
        ev["report"]["trades"] = 99
        assert any("content_sha256" in p for p in et.validate_evidence(ev))

    def test_single_bar_report_is_strict_json(self):
        ev = et.build_evidence(_frame([_t("A", "BUY", 0, 0)]), ASSUMPTIONS, {"run_dir": "x"})
        json.dumps(ev, allow_nan=False)
        assert np.isfinite(ev["report"]["occupancy"]["concurrent_positions_time_weighted"]["mean"])
