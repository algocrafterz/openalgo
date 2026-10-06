"""The recorded headline is statutory, per trade, at the deployment profile's broker.

Every metric, split, position cap and gate reads the trades a run carries. A recorded run
restates those trades once, at the source (`record_run.statutory_execution`), so the headline
is the statutory-per-trade net and the flat engine figure is only a comparison scenario.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from portfolio.costs import india_delivery, india_intraday
from signal_engine.backtest import costs, tearsheet
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.concurrency import apply_position_cap
from signal_engine.backtest.types import Trade
from signal_engine.research.intake.fingerprints import canonical_json, sha256_hex
from signal_engine.tests.cost_fixtures import make_model

FLAT = {"model": "flat-round-trip-bps", "round_trip_bps": 16.0}


def _day(i: int) -> date:
    return date(2026, 1, 5) + timedelta(days=i + 2 * (i // 5))


def _trade(i: int, r_gross: float, *, risk: float = 1.0, no_stop: bool = False) -> Trade:
    day = _day(i)
    kw = {"gross_ret": r_gross / 100.0, "net_ret": r_gross / 100.0} if no_stop else {}
    return Trade(
        symbol=f"S{i % 4}.NS",
        day=day,
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=float("nan") if no_stop else risk,
        exit_time=pd.Timestamp(f"{day} 15:00"),
        reason="tp",
        r_gross=float("nan") if no_stop else r_gross,
        r_net=float("nan") if no_stop else r_gross - 0.16,
        **kw,
    )


def _book(n=40, gross=0.25, **kw):
    return [_trade(i, gross if i % 4 else -0.5, **kw) for i in range(n)]


def _record(trades, model, flat=16.0, reference=(), **kw):
    restated, block = rr.statutory_execution(trades, model, flat, reference)
    record = tearsheet.build_run_record(
        restated,
        run_id="2026-10-07-x-v1",
        run_date="2026-10-07",
        code_version="c",
        data={"source": "s", "as_of": "2026-03-01", "universe": "u", "bar_size": "5m"},
        period={"start": "2026-01-05", "end": "2026-03-01"},
        costs=block,
        cost_model=model,
        n_trials=1,
        **kw,
    )
    return record, restated


def _row(record, gate):
    rows = [r for d in record["evaluation"]["dimensions"] for r in d["rows"] if r["gate"] == gate]
    assert len(rows) == 1
    return rows[0]


class TestHeadlineIsStatutory:
    def test_headline_net_is_statutory_intraday(self):
        record, _ = _record(_book(), make_model())
        model_block = record["cost_model"]
        assert record["metrics"]["expectancy_r"] == pytest.approx(model_block["net_r"], abs=1e-4)
        assert record["costs"]["model"] == "statutory-per-trade"
        assert record["costs"]["round_trip_bps"] == model_block["mean_total_bps"]
        assert record["costs"]["flat_round_trip_bps"] == 16.0
        assert record["costs"]["round_trip_bps"] != 16.0

    def test_g12_uses_statutory_net(self):
        # Gross 0.25R on a 1 percent stop. At the flat 16 bps the net is +0.09R and passes; with
        # 15 bps slippage a side the statutory cost is about 0.33R and the same book fails.
        trades = [_trade(i, 0.25) for i in range(40)]
        flat = tearsheet.build_run_record(
            [dataclasses.replace(t, r_net=t.r_gross - 0.16) for t in trades],
            run_id="2026-10-07-x-v1",
            run_date="2026-10-07",
            code_version="c",
            data={"source": "s", "as_of": "2026-03-01", "universe": "u", "bar_size": "5m"},
            period={"start": "2026-01-05", "end": "2026-03-01"},
            costs=FLAT,
            n_trials=1,
        )
        assert _row(flat, "G1.2")["status"] == "pass"
        record, _ = _record(trades, make_model(slippage_bps_per_side=15.0))
        row = _row(record, "G1.2")
        assert row["status"] == "fail"
        assert "statutory-per-trade, broker flattrade" in row["evidence"]

    def test_g14_ratio_uses_primary_mean(self):
        record, _ = _record(_book(), make_model())
        row = _row(record, "G1.4")
        gross = record["metrics"]["gross_bps"]
        mean = record["costs"]["round_trip_bps"]
        assert row["value"] == pytest.approx(gross / mean, abs=1e-3)
        assert "legacy flat 16 bps" in row["evidence"]
        assert "secondary" in row["evidence"]

    def test_g15_median_cost_is_statutory(self):
        record, restated = _record(_book(), make_model())
        row = _row(record, "G1.5")
        expected = float(np.median([t.r_gross - t.r_net for t in restated]))
        assert row["value"] == pytest.approx(expected, abs=1e-3)
        assert row["value"] != pytest.approx(0.16, abs=1e-3)
        assert "statutory-per-trade" in row["evidence"]

    def test_split_and_position_caps_on_statutory_basis(self):
        record, restated = _record(
            _book(), make_model(), oos_start="2026-02-09", position_caps=(3,)
        )
        before = [t for t in restated if t.day.isoformat() < "2026-02-09"]
        after = [t for t in restated if t.day.isoformat() >= "2026-02-09"]
        assert record["in_sample"]["net_bps"] == pytest.approx(
            np.mean([t.net_bps for t in before]), abs=1e-3
        )
        assert record["out_of_sample"]["net_bps"] == pytest.approx(
            np.mean([t.net_bps for t in after]), abs=1e-3
        )
        taken = apply_position_cap(restated, 3)
        assert record["position_caps"][0]["net_bps"] == pytest.approx(
            np.mean([t.net_bps for t in taken]), abs=1e-3
        )

    def test_swing_no_stop_headline_is_statutory_bps(self):
        trades = [_trade(i, 2.0 if i % 3 else -1.5, no_stop=True) for i in range(30)]
        model = make_model(product="CNC", leverage=1.0, fixed_notional=100_000.0)
        record, restated = _record(trades, model, flat=24.0)
        profile = record["cost_model"]
        assert "net_r" not in profile and "net_bps" in profile
        assert record["metrics"]["net_bps"] == pytest.approx(profile["net_bps"], abs=1e-3)
        expected = np.mean([t.gross_ret - model.trade_cost_bps(t) / 1e4 for t in trades]) * 1e4
        assert record["metrics"]["net_bps"] == pytest.approx(expected, abs=1e-3)
        assert record["costs"]["round_trip_bps"] == profile["mean_total_bps"]
        assert record["costs"]["flat_round_trip_bps"] == 24.0

    def test_build_run_record_rejects_statutory_costs_with_flat_trades(self):
        model = make_model()
        trades = _book()
        _, block = rr.statutory_execution(trades, model, 16.0)
        with pytest.raises(ValueError, match="not restated"):
            tearsheet.build_run_record(
                trades,
                run_id="2026-10-07-x-v1",
                run_date="2026-10-07",
                code_version="c",
                data={"source": "s", "as_of": "2026-03-01", "universe": "u", "bar_size": "5m"},
                period={"start": "2026-01-05", "end": "2026-03-01"},
                costs=block,
                cost_model=model,
                n_trials=1,
            )

    def test_pnl_basis_does_not_say_no_stop_for_trades_with_stops(self):
        trades = _book()
        profile = costs.cost_profile(trades, make_model(fixed_notional=100_000.0), flat_bps=16.0)
        assert "no stop" not in profile["pnl_basis"]


class TestBroker:
    def _components(self, product, **kw):
        model = make_model(product=product, fixed_notional=100_000.0, **kw)
        frame = costs.decompose_trades([_trade(0, 1.0)], model)
        return {c: float(frame[c].iloc[0]) for c in frame.columns}

    def test_flattrade_profile_charges_zero_brokerage_intraday(self):
        comps = self._components("MIS")
        assert comps["cost_brokerage_bps"] == 0.0
        for line in ("stt", "exchange", "gst", "sebi", "stamp"):
            assert comps[f"cost_{line}_bps"] > 0.0

    def test_flattrade_profile_charges_zero_brokerage_delivery(self):
        comps = self._components("CNC", leverage=1.0)
        # Brokerage is nil; what remains in that column is the DP charge and its GST.
        dp_bps = 20.0 * 1.18 / 100_000.0 * 1e4
        assert comps["cost_brokerage_bps"] == pytest.approx(dp_bps)
        for line in ("stt", "exchange", "gst", "sebi", "stamp"):
            assert comps[f"cost_{line}_bps"] > 0.0
        total = costs.decompose_trades(
            [_trade(0, 1.0)], make_model(product="CNC", leverage=1.0, fixed_notional=100_000.0)
        )["modeled_cost_bps"].iloc[0]
        statutory = india_delivery("NSE", broker="flattrade").charge(100_000.0, 100_000.0, orders=2)
        assert total == pytest.approx((statutory + 20.0 * 1.18) / 100_000.0 * 1e4 + 8.0)

    def test_generic_scenario_charges_default_brokerage_per_trade(self):
        trades = _book()
        model = make_model(fixed_notional=100_000.0)
        profile = costs.cost_profile(trades, model, flat_bps=16.0)
        by_id = {s["id"]: s for s in profile["scenarios"]}
        generic = by_id["generic_discount_broker"]
        default_bps = india_intraday("NSE").charge(100_000.0, 100_000.0, orders=2) / 100_000.0 * 1e4
        assert generic["mean_round_trip_bps"] == pytest.approx(default_bps + 8.0, abs=1e-3)
        assert generic["mean_round_trip_bps"] != 16.0
        # It differs from the primary by exactly the brokerage and its GST.
        brokerage = 20.0 * 2 * 1.18 / 100_000.0 * 1e4
        gap = generic["mean_round_trip_bps"] - by_id["primary"]["mean_round_trip_bps"]
        assert gap == pytest.approx(brokerage, abs=1e-3)

    def test_unknown_or_missing_broker_records_assumed_default(self):
        for broker in (None, "some-new-broker"):
            _, block = rr.statutory_execution(_book(), make_model(broker=broker), 16.0)
            assert block["broker"]["assumed"] is True
            assert block["broker"]["key"] == "default"
        _, known = rr.statutory_execution(_book(), make_model(), 16.0)
        assert known["broker"]["assumed"] is False

    def test_cost_record_names_broker_and_version(self):
        record, _ = _record(_book(), make_model())
        block = record["costs"]
        assert block["broker"] == {
            "key": "flattrade",
            "label": "Flattrade",
            "source": "https://flattrade.in/charges/",
            "verified_on": "2026-10-01",
            "assumed": False,
        }
        assert block["cost_model_version"] == costs.COST_MODEL_VERSION
        assert block["rate_table_sha256"] == record["cost_model"]["rate_table_sha256"]
        assert block["reference_round_trip_bps"] == []


class TestScenarios:
    def test_scenarios_reported_side_by_side(self):
        record, _ = _record(_book(), make_model(), reference=(32.0,))
        by_id = {s["id"]: s for s in record["cost_model"]["scenarios"]}
        assert list(by_id) == ["primary", "generic_discount_broker", "flat_32bps", "legacy_flat"]
        for scenario in by_id.values():
            for key in (
                "definition",
                "mean_round_trip_bps",
                "net_bps",
                "net_r",
                "net_rs",
                "win_rate",
                "t_stat",
            ):
                assert key in scenario, (scenario["id"], key)
        for scenario_id in ("primary", "generic_discount_broker"):
            assert set(by_id[scenario_id]["slippage_stress"]) == {"1.5", "2.0"}
        assert "slippage_stress" not in by_id["flat_32bps"]
        assert by_id["flat_32bps"]["mean_round_trip_bps"] == 32.0
        assert by_id["legacy_flat"]["mean_round_trip_bps"] == 16.0
        assert by_id["primary"]["net_r"] == record["cost_model"]["net_r"]
        assert by_id["primary"]["slippage_stress"]["2.0"]["net_r"] == pytest.approx(
            record["cost_model"]["net_r_slip_2x"]
        )
        # A dearer scenario nets less.
        assert (
            by_id["primary"]["net_bps"]
            > by_id["generic_discount_broker"]["net_bps"]
            > by_id["legacy_flat"]["net_bps"]
            > by_id["flat_32bps"]["net_bps"]
        )

    def test_reference_levels_reach_the_cost_sensitivity_grid(self):
        record, _ = _record(_book(), make_model(), reference=(32.0,))
        levels = {r["round_trip_bps"] for r in record["cost_sensitivity"]}
        assert {0, 5, 10, 20, 16.0, 32.0, record["costs"]["round_trip_bps"]} <= levels

    def test_scenarios_sha256_recorded_and_stable(self):
        a, _ = _record(_book(), make_model(), reference=(32.0,))
        b, _ = _record(_book(), make_model(), reference=(32.0,))
        block = a["cost_model"]
        assert block["scenarios_sha256"] == b["cost_model"]["scenarios_sha256"]
        definitions = [{"id": s["id"], "definition": s["definition"]} for s in block["scenarios"]]
        assert block["scenarios_sha256"] == sha256_hex(canonical_json(definitions))
        other, _ = _record(_book(), make_model(), reference=(48.0,))
        assert other["cost_model"]["scenarios_sha256"] != block["scenarios_sha256"]

    def test_only_primary_drives_gates(self):
        plain, _ = _record(_book(), make_model())
        extra, _ = _record(_book(), make_model(), flat=99.0, reference=(500.0,))
        assert plain["metrics"] == extra["metrics"]
        for gate in [r["gate"] for d in plain["evaluation"]["dimensions"] for r in d["rows"]]:
            a, b = _row(plain, gate), _row(extra, gate)
            assert (a["status"], a["value"]) == (b["status"], b["value"]), gate


class TestDpCharge:
    def test_dp_charge_is_in_the_cnc_rate_table_only(self, monkeypatch):
        from portfolio import broker_fees

        before_cnc = costs.model_rate_table_sha256(make_model(product="CNC", leverage=1.0))
        before_mis = costs.model_rate_table_sha256(make_model())
        before_id = costs.cost_identity(make_model(product="CNC", leverage=1.0))
        assert costs.rate_table("CNC", "NSE", "flattrade")["charges"]["dp_charge"]["flat"] == 20.0
        assert "dp_charge" not in costs.rate_table("MIS", "NSE", "flattrade")["charges"]
        fees = dataclasses.replace(broker_fees.BROKER_FEES["flattrade"], dp_charge=25.0)
        monkeypatch.setitem(broker_fees.BROKER_FEES, "flattrade", fees)
        after_id = costs.cost_identity(make_model(product="CNC", leverage=1.0))
        assert costs.model_rate_table_sha256(make_model(product="CNC", leverage=1.0)) != before_cnc
        assert after_id != before_id
        assert costs.model_rate_table_sha256(make_model()) == before_mis

    def test_a_short_cnc_trade_pays_no_dp_charge(self):
        model = make_model(product="CNC", leverage=1.0, fixed_notional=100_000.0)
        long_t = _trade(0, 1.0)
        short_t = dataclasses.replace(long_t, direction=-1)
        gap = model.statutory_bps(long_t) - model.statutory_bps(short_t)
        assert gap == pytest.approx(20.0 * 1.18 / 100_000.0 * 1e4)
        frame = costs.decompose_trades([long_t, short_t], model)
        assert frame["cost_brokerage_bps"].iloc[1] == 0.0
