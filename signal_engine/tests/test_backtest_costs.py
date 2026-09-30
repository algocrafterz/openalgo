"""Per-trade statutory cost model at the deployed notional (G1.1, G1.3)."""

import pytest

from portfolio.costs import india_delivery, india_intraday
from signal_engine.backtest import costs
from signal_engine.backtest.types import Trade


def _trade(entry=100.0, risk=1.0, r_gross=1.0, r_net=0.9, direction=1):
    return Trade(
        symbol="A.NS",
        day="2026-01-05",
        direction=direction,
        tag="t",
        entry_time="2026-01-05 09:20",
        entry=entry,
        signal_price=entry,
        sl=entry - risk,
        tp=0.0,
        risk=risk,
        exit_time="2026-01-05 14:30",
        exit=entry + risk * r_gross,
        r_gross=r_gross,
        r_net=r_net,
    )


def _model(**kw):
    base = {
        "capital": 100_000.0,
        "risk_per_trade": 0.01,
        "leverage": 5.0,
        "product": "MIS",
        "exchange": "NSE",
        "slippage_bps_per_side": 4.0,
    }
    return costs.CostModel(**{**base, **kw})


class TestNotional:
    def test_risk_based_notional(self):
        # risk Rs 1000 on a 1% stop -> Rs 1,00,000 of stock
        assert _model().notional(_trade(entry=100.0, risk=1.0)) == pytest.approx(100_000.0)

    def test_wide_stop_shrinks_notional(self):
        assert _model().notional(_trade(entry=100.0, risk=4.0)) == pytest.approx(25_000.0)

    def test_leverage_caps_notional(self):
        # a 0.1% stop would size Rs 10 lakh; MIS leverage 5x on Rs 1 lakh caps it at 5 lakh
        assert _model().notional(_trade(entry=100.0, risk=0.1)) == pytest.approx(500_000.0)

    def test_delivery_is_not_levered(self):
        m = _model(product="CNC", leverage=1.0)
        assert m.notional(_trade(entry=100.0, risk=0.1)) == pytest.approx(100_000.0)


class TestCostBps:
    def test_intraday_matches_the_schedule_plus_two_slippage_legs(self):
        n = 100_000.0
        statutory = india_intraday("NSE").charge(buy_value=n, sell_value=n, orders=2) / n * 1e4
        got = _model().trade_cost_bps(_trade(entry=100.0, risk=1.0))
        assert got == pytest.approx(statutory + 8.0, rel=1e-6)

    def test_delivery_uses_the_delivery_schedule(self):
        n = 100_000.0
        statutory = india_delivery("NSE").charge(buy_value=n, sell_value=n, orders=2) / n * 1e4
        m = _model(product="CNC", leverage=1.0)
        assert m.trade_cost_bps(_trade(entry=100.0, risk=1.0)) == pytest.approx(
            statutory + 8.0, rel=1e-6
        )

    def test_stressing_slippage_changes_only_the_slippage_part(self):
        m = _model()
        base = m.trade_cost_bps(_trade())
        doubled = m.trade_cost_bps(_trade(), slippage_mult=2.0)
        assert doubled - base == pytest.approx(8.0, rel=1e-6)

    def test_small_notional_pays_more_bps_when_brokerage_is_flat(self):
        m = _model()
        big = m.trade_cost_bps(_trade(entry=100.0, risk=1.0))
        small = m.trade_cost_bps(_trade(entry=100.0, risk=20.0))
        assert small != big

    def test_unknown_product_is_rejected(self):
        with pytest.raises(ValueError):
            _model(product="NRML")


class TestRecost:
    def test_net_is_gross_minus_cost_in_r(self):
        m = _model()
        t = _trade(entry=100.0, risk=1.0, r_gross=1.0, r_net=0.0)
        (out,) = costs.recost([t], m)
        bps = m.trade_cost_bps(t)
        assert out.r_net == pytest.approx(1.0 - bps / 1e4 * 100.0 / 1.0)

    def test_inputs_are_not_modified(self):
        t = _trade(r_net=0.123)
        costs.recost([t], _model())
        assert t.r_net == 0.123

    def test_slippage_stress_lowers_net(self):
        t = _trade()
        (a,) = costs.recost([t], _model())
        (b,) = costs.recost([t], _model(), slippage_mult=2.0)
        assert b.r_net < a.r_net


class TestProfile:
    def test_profile_reports_stress_levels(self):
        trades = [_trade(r_gross=1.0) for _ in range(10)]
        p = costs.cost_profile(trades, _model(), flat_bps=16.0)
        assert p["model"] == "statutory-per-trade"
        assert p["flat_bps"] == 16.0
        assert p["mean_total_bps"] > p["mean_statutory_bps"]
        assert p["net_r_slip_2x"] < p["net_r"]
        assert set(p["total_bps_at"]) == {"1.0", "1.5", "2.0"}

    def test_empty_trades_give_none(self):
        assert costs.cost_profile([], _model(), flat_bps=16.0) is None

    def test_model_parameters_are_recorded(self):
        p = costs.cost_profile([_trade()], _model(), flat_bps=16.0)
        assert p["capital"] == 100_000.0 and p["product"] == "MIS" and p["leverage"] == 5.0
