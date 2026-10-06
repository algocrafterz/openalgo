"""Cost model for trades with no stop: fixed notional sizing, and results kept in bps."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import costs
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br

FIXED = 100_000.0


def model(**kw):
    base = {
        "capital": 2_000_000.0,
        "risk_per_trade": 0.01,
        "leverage": 1.0,
        "product": "CNC",
        "exchange": "NSE",
        "slippage_bps_per_side": 4.0,
        "fixed_notional": FIXED,
    }
    return costs.CostModel(**{**base, **kw})


def no_stop(gross_ret=0.05, entry=250.0, symbol="A.NS"):
    return Trade(
        symbol=symbol,
        day="2026-01-05",
        direction=1,
        tag="t",
        entry_time=pd.Timestamp("2026-01-05 09:15"),
        entry=entry,
        signal_price=entry,
        sl=float("nan"),
        tp=math.inf,
        risk=float("nan"),
        exit_time="2026-02-05 09:15",
        exit=entry * (1 + gross_ret),
        reason="REBALANCE",
        r_gross=float("nan"),
        r_net=float("nan"),
        gross_ret=gross_ret,
        net_ret=gross_ret - 0.0024,
    )


def stop_based(entry=100.0, risk=1.0):
    return Trade(
        symbol="B.NS",
        day="2026-01-05",
        direction=1,
        tag="t",
        entry_time="2026-01-05 09:15",
        entry=entry,
        signal_price=entry,
        sl=entry - risk,
        tp=0.0,
        risk=risk,
        exit_time="2026-01-05 14:30",
        exit=entry + 2 * risk,
        r_gross=2.0,
        r_net=1.9,
    )


class TestFixedNotional:
    def test_the_notional_is_the_fixed_amount_for_a_trade_without_a_stop(self):
        assert model().notional(no_stop()) == FIXED

    def test_it_does_not_depend_on_price(self):
        assert model().notional(no_stop(entry=20.0)) == model().notional(no_stop(entry=9000.0))

    def test_without_it_a_trade_without_a_stop_cannot_be_sized(self):
        with pytest.raises(ValueError, match="stop"):
            model(fixed_notional=None).notional(no_stop())

    def test_without_it_a_stop_based_trade_is_sized_as_before(self):
        m = model(fixed_notional=None, capital=100_000.0)
        assert m.notional(stop_based()) == pytest.approx(100_000.0)

    def test_it_must_be_positive(self):
        with pytest.raises(ValueError, match="fixed_notional"):
            model(fixed_notional=0.0)

    def test_the_cost_in_bps_uses_the_fixed_notional_bucket(self):
        m = model()
        t = no_stop()
        assert m.trade_cost_bps(t) == pytest.approx(m.statutory_bps(t) + 8.0)


class TestRecostAndDecompose:
    def test_recost_restates_the_return_not_r(self):
        m = model()
        (out,) = costs.recost([no_stop(gross_ret=0.05)], m)
        assert out.net_ret == pytest.approx(0.05 - m.trade_cost_bps(no_stop()) / 1e4)
        assert math.isnan(out.r_net)

    def test_slippage_stress_scales_only_slippage(self):
        m = model()
        (a,) = costs.recost([no_stop()], m)
        (b,) = costs.recost([no_stop()], m, slippage_mult=2.0)
        assert a.net_ret - b.net_ret == pytest.approx(2 * 4.0 / 1e4)

    def test_decompose_matches_the_per_trade_components(self):
        m = model()
        trades = [no_stop(entry=250.0), no_stop(entry=1800.0)]
        frame = costs.decompose_trades(trades, m)
        for i, t in enumerate(trades):
            assert frame["modeled_cost_bps"].iloc[i] == pytest.approx(
                m.components(t)["modeled_cost_bps"]
            )

    def test_stop_based_recost_is_unchanged(self):
        m = model(fixed_notional=None, capital=100_000.0)
        (out,) = costs.recost([stop_based()], m)
        assert out.r_net == pytest.approx(2.0 - m.trade_cost_bps(stop_based()) / 1e4 * 100.0 / 1.0)


class TestProfile:
    @pytest.fixture
    def trades(self):
        return [no_stop(0.05), no_stop(-0.03), no_stop(0.02)]

    def test_results_are_in_bps_and_there_are_no_r_keys(self, trades):
        profile = costs.cost_profile(trades, model(), flat_bps=24.0)
        assert "net_r" not in profile and "net_r_slip_2x" not in profile
        costed = costs.recost(trades, model())
        assert profile["net_bps"] == pytest.approx(
            np.mean([t.net_ret for t in costed]) * 1e4, abs=1e-2
        )
        stressed = costs.recost(trades, model(), slippage_mult=2.0)
        assert profile["net_bps_slip_2x"] == pytest.approx(
            np.mean([t.net_ret for t in stressed]) * 1e4, abs=1e-2
        )

    def test_rupee_pnl_is_the_fixed_notional_times_the_net_return(self, trades):
        profile = costs.cost_profile(trades, model(), flat_bps=24.0)
        costed = costs.recost(trades, model())
        assert profile["modelled_net_pnl_rs"] == pytest.approx(
            sum(FIXED * t.net_ret for t in costed), abs=0.01
        )
        total_cost = sum(FIXED * model().trade_cost_bps(t) / 1e4 for t in trades)
        assert profile["modelled_total_cost_rs"] == pytest.approx(total_cost, abs=0.01)

    def test_the_basis_names_the_fixed_notional_and_says_modelled(self, trades):
        basis = costs.cost_profile(trades, model(), flat_bps=24.0)["pnl_basis"]
        assert basis.startswith("modelled")
        assert "fixed" in basis and "1,00,000" in basis
        assert "stop" not in basis.lower().replace("no stop", "")

    def test_the_assumptions_are_stored_with_the_numbers(self, trades):
        profile = costs.cost_profile(trades, model(), flat_bps=24.0)
        assert profile["fixed_notional"] == FIXED
        assert profile["capital"] == 2_000_000.0 and profile["leverage"] == 1.0

    def test_the_registry_accepts_the_bps_profile(self, trades):
        profile = costs.cost_profile(trades, model(), flat_bps=24.0)
        assert br._validate_cost_model(profile) == []

    def test_a_stop_based_profile_is_unchanged(self):
        m = model(fixed_notional=None, capital=100_000.0)
        profile = costs.cost_profile([stop_based()], m, flat_bps=24.0)
        assert "net_r" in profile and "net_bps" not in profile
        assert "fixed_notional" not in profile
        assert br._validate_cost_model(profile) == []


class TestCostProfileRecordsTheRateTable:
    def profile(self, **kw):
        trades = [no_stop(0.01), no_stop(-0.01, symbol="B.NS")]
        return costs.cost_profile(trades, model(product="MIS", **kw), flat_bps=16.0)

    def test_the_model_basis_notional_and_stress_levels_are_recorded(self):
        p = self.profile()
        assert (p["model"], p["product"], p["exchange"]) == ("statutory-per-trade", "MIS", "NSE")
        assert p["notional_basis"] == "fixed_notional" and p["fixed_notional"] == FIXED
        assert p["slippage_bps_per_side"] == 4.0
        assert p["stress_levels"] == [1.0, 1.5, 2.0]
        assert set(p["total_bps_at"]) == {"1.0", "1.5", "2.0"}

    def test_the_risk_basis_is_labelled_for_a_stop_sized_model(self):
        p = costs.cost_profile([stop_based()], model(product="MIS", fixed_notional=None), 16.0)
        assert p["notional_basis"] == "risk" and "fixed_notional" not in p

    def test_the_statutory_rate_lines_are_the_intraday_schedule(self):
        table = self.profile()["rate_table"]
        assert table["tax_label"] == "GST" and table["tax_rate"] == 0.18
        assert set(table["charges"]) == {"brokerage", "stt", "exchange_txn", "sebi", "stamp_duty"}
        assert table["charges"]["stt"]["basis"] == "sell"
        assert table["charges"]["stt"]["rate"] == 0.00025
        assert table["charges"]["brokerage"]["cap"] == 20.0

    def test_the_hash_identifies_the_table_and_follows_any_change(self):
        p = self.profile()
        assert p["rate_table_sha256"] == costs.rate_table_sha256(p["rate_table"])
        assert len(p["rate_table_sha256"]) == 64
        changed = {**p["rate_table"], "tax_rate": 0.19}
        assert costs.rate_table_sha256(changed) != p["rate_table_sha256"]
        assert costs.rate_table_sha256(costs.rate_table("MIS", "NSE")) == p["rate_table_sha256"]
        assert costs.rate_table_sha256(costs.rate_table("CNC", "NSE")) != p["rate_table_sha256"]
