"""Cost model version 2: every trade is priced from its own entry and exit legs.

Entry value is the sized notional. Exit value is notional x exit / entry. A long buys at entry and
sells at exit; a short sells at entry and buys at exit. Brokerage is capped per order, so each leg
is priced on its own value, never on the average of the two. STT (intraday: sell leg) and stamp duty
(buy leg) follow the real leg values.

Hand calculations use the generic discount broker (0.03 percent or Rs 20 per order, whichever is
lower; broker=None), NSE: exchange txn 0.00307 percent of turnover, STT 0.025 percent on the sell leg,
stamp 0.003 percent on the buy leg, SEBI Rs 10 per crore of turnover, GST 18 percent on brokerage +
exchange txn + SEBI.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pandas as pd
import pytest

from portfolio.broker_fees import fees_for
from portfolio.costs import india_delivery, india_intraday
from signal_engine.backtest import costs
from signal_engine.backtest.costs import CostModel, CostModelUnavailable, decompose_trades, recost
from signal_engine.backtest.types import Trade


def _model(**kw) -> CostModel:
    base: dict[str, Any] = {
        "capital": 1_000_000.0,
        "risk_per_trade": 0.01,
        "leverage": 5.0,
        "product": "MIS",
        "exchange": "NSE",
        "slippage_bps_per_side": 4.0,
        "fixed_notional": 100_000.0,
        "broker": None,
    }
    base.update(kw)
    return CostModel(**base)


def _trade(entry=100.0, exit_=100.0, direction=1, risk=1.0) -> Trade:
    return Trade(
        symbol="AAA",
        day=pd.Timestamp("2026-06-01").date(),
        direction=direction,
        tag="t",
        entry_time=pd.Timestamp("2026-06-01 09:25"),
        entry=entry,
        signal_price=entry,
        sl=entry - direction * risk,
        tp=np.nan,
        risk=risk,
        exit=exit_,
        r_gross=0.0,
        r_net=0.0,
    )


def _rupees(model: CostModel, trade: Trade) -> dict[str, float]:
    """Statutory lines in rupees (slippage column excluded), from the model's bps lines."""
    notional = model.notional(trade)
    comp = model.components(trade, slippage_mult=0.0)
    return {k: v * notional / 1e4 for k, v in comp.items()}


class TestHandCalculatedLegs:
    def test_balanced_long_round_trip(self):
        # buy 1,00,000 / sell 1,00,000 (exit == entry)
        # brokerage: min(0.03% x 1,00,000 = 30, 20) = 20 per leg           -> 40.00
        # STT: 0.025% x 1,00,000 sell                                      -> 25.00
        # exchange: 0.00307% x 2,00,000                                    ->  6.14
        # SEBI: Rs 10/crore x 2,00,000                                     ->  0.20
        # stamp: 0.003% x 1,00,000 buy                                     ->  3.00
        # GST: 18% x (40 + 6.14 + 0.20) = 18% x 46.34                      ->  8.3412
        # total = 82.6812
        r = _rupees(_model(), _trade(100.0, 100.0))
        assert r["cost_brokerage_bps"] == pytest.approx(40.0, abs=1e-9)
        assert r["cost_stt_bps"] == pytest.approx(25.0, abs=1e-9)
        assert r["cost_exchange_bps"] == pytest.approx(6.14, abs=1e-9)
        assert r["cost_sebi_bps"] == pytest.approx(0.20, abs=1e-9)
        assert r["cost_stamp_bps"] == pytest.approx(3.0, abs=1e-9)
        assert r["cost_gst_bps"] == pytest.approx(8.3412, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(82.6812, abs=1e-9)

    def test_unequal_legs_cap_binds_on_one_leg_only(self):
        # long, notional 50,000, exit/entry = 1.8 -> buy 50,000 / sell 90,000
        # brokerage: buy min(15, 20) = 15.00; sell min(27, 20) = 20.00     -> 35.00
        #   (the old averaged cap gave min(21, 20) x 2 = 40.00)
        # STT: 0.025% x 90,000 sell                                        -> 22.50
        # exchange: 0.00307% x 1,40,000                                    ->  4.298
        # SEBI: 1e-6 x 1,40,000                                            ->  0.14
        # stamp: 0.003% x 50,000 buy                                       ->  1.50
        # GST: 18% x (35 + 4.298 + 0.14) = 18% x 39.438                    ->  7.09884
        # total = 70.53684
        m = _model(fixed_notional=50_000.0)
        r = _rupees(m, _trade(100.0, 180.0))
        assert r["cost_brokerage_bps"] == pytest.approx(35.0, abs=1e-9)
        assert r["cost_stt_bps"] == pytest.approx(22.5, abs=1e-9)
        assert r["cost_exchange_bps"] == pytest.approx(4.298, abs=1e-9)
        assert r["cost_sebi_bps"] == pytest.approx(0.14, abs=1e-9)
        assert r["cost_stamp_bps"] == pytest.approx(1.5, abs=1e-9)
        assert r["cost_gst_bps"] == pytest.approx(7.09884, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(70.53684, abs=1e-9)
        # portfolio.costs used to average the legs against the per-order cap and
        # gave 76.43684 (5.90 too high). Since the leg-cap fix, orders=2 with both
        # legs nonzero prices each leg as its own order, so it equals the legwise
        # result for this case. Intended behaviour change, not a regression.
        schedule_total = india_intraday("NSE").charge(50_000.0, 90_000.0, 2)
        assert schedule_total == pytest.approx(70.53684, abs=1e-9)
        assert schedule_total == pytest.approx(r["modeled_cost_bps"], abs=1e-9)

    def test_short_sells_at_entry_and_buys_at_exit(self):
        # short, entry 180, exit 100, notional 90,000 -> sell 90,000 / buy 50,000
        # brokerage: sell min(27, 20) = 20; buy min(15, 20) = 15           -> 35.00
        # STT: 0.025% x 90,000 sell                                        -> 22.50
        # exchange 4.298, SEBI 0.14 (turnover 1,40,000)
        # stamp: 0.003% x 50,000 buy                                       ->  1.50
        # GST 7.09884; total = 70.53684
        m = _model(fixed_notional=90_000.0)
        r = _rupees(m, _trade(180.0, 100.0, direction=-1))
        assert r["cost_stt_bps"] == pytest.approx(22.5, abs=1e-9)
        assert r["cost_stamp_bps"] == pytest.approx(1.5, abs=1e-9)
        assert r["cost_brokerage_bps"] == pytest.approx(35.0, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(70.53684, abs=1e-9)

    def test_long_with_the_same_two_values_swapped_legs_prices_differently(self):
        # long, entry 180, exit 100, notional 90,000 -> buy 90,000 / sell 50,000
        # STT: 0.025% x 50,000 = 12.50; stamp: 0.003% x 90,000 = 2.70
        # brokerage 20 + 15 = 35; exchange 4.298; SEBI 0.14; GST 7.09884
        # total = 35 + 12.5 + 4.298 + 0.14 + 2.7 + 7.09884 = 61.73684
        m = _model(fixed_notional=90_000.0)
        r = _rupees(m, _trade(180.0, 100.0, direction=1))
        assert r["cost_stt_bps"] == pytest.approx(12.5, abs=1e-9)
        assert r["cost_stamp_bps"] == pytest.approx(2.7, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(61.73684, abs=1e-9)

    def test_tiny_trade_neither_leg_reaches_the_cap(self):
        # notional 10,000, exit == entry
        # brokerage: 0.03% x 10,000 = 3.00 per leg                         ->  6.00
        # STT: 0.025% x 10,000                                             ->  2.50
        # exchange: 0.00307% x 20,000                                      ->  0.614
        # SEBI: 1e-6 x 20,000                                              ->  0.02
        # stamp: 0.003% x 10,000                                           ->  0.30
        # GST: 18% x (6 + 0.614 + 0.02) = 18% x 6.634                      ->  1.19412
        # total = 10.62812
        r = _rupees(_model(fixed_notional=10_000.0), _trade(100.0, 100.0))
        assert r["cost_brokerage_bps"] == pytest.approx(6.0, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(10.62812, abs=1e-9)

    def test_cnc_long_flattrade(self):
        # buy 1,00,000 / sell 1,02,000 (entry 100, exit 102); Flattrade: no delivery brokerage,
        # DP charge Rs 20 + 18% GST = 23.60 on the sell
        # STT: 0.1% x 2,02,000 (both legs)                                 -> 202.00
        # exchange: 0.00307% x 2,02,000                                    ->   6.2014
        # SEBI: 1e-6 x 2,02,000                                            ->   0.202
        # stamp: 0.015% x 1,00,000 buy                                     ->  15.00
        # GST: 18% x (0 + 6.2014 + 0.202) = 18% x 6.4034                   ->   1.152612
        # DP incl. GST                                                     ->  23.60
        # total = 248.156012  (sum below)
        m = _model(product="CNC", broker="flattrade", leverage=1.0)
        r = _rupees(m, _trade(100.0, 102.0))
        assert r["cost_stt_bps"] == pytest.approx(202.0, abs=1e-9)
        assert r["cost_stamp_bps"] == pytest.approx(15.0, abs=1e-9)
        assert r["cost_exchange_bps"] == pytest.approx(6.2014, abs=1e-9)
        assert r["cost_brokerage_bps"] == pytest.approx(23.6, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(
            202.0 + 6.2014 + 0.202 + 15.0 + 1.152612 + 23.6, abs=1e-9
        )

    def test_cnc_long_generic_broker_has_no_brokerage_or_dp(self):
        # same legs, generic broker: brokerage 0, DP 0
        # total = 202 + 6.2014 + 0.202 + 15 + 1.152612 = 224.556012
        m = _model(product="CNC", broker=None, leverage=1.0)
        r = _rupees(m, _trade(100.0, 102.0))
        assert r["cost_brokerage_bps"] == pytest.approx(0.0, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(224.556012, abs=1e-9)

    def test_cnc_short_pays_stt_both_legs_stamp_on_the_exit_buy_and_no_dp(self):
        # short entry 100, exit 98, notional 1,00,000 -> sell 1,00,000 / buy 98,000
        # STT 0.1% x 1,98,000 = 198.00; exchange 0.00307% x 1,98,000 = 6.0786
        # SEBI 1e-6 x 1,98,000 = 0.198; stamp 0.015% x 98,000 = 14.70
        # GST 18% x (6.0786 + 0.198) = 1.129788; no DP charge on a short
        # total = 198 + 6.0786 + 0.198 + 14.7 + 1.129788 = 220.106388
        m = _model(product="CNC", broker="flattrade", leverage=1.0)
        r = _rupees(m, _trade(100.0, 98.0, direction=-1))
        assert r["cost_stamp_bps"] == pytest.approx(14.7, abs=1e-9)
        assert r["cost_brokerage_bps"] == pytest.approx(0.0, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(220.106388, abs=1e-9)

    def test_flattrade_intraday_has_zero_brokerage(self):
        # balanced 1,00,000: STT 25 + exchange 6.14 + SEBI 0.20 + stamp 3 + GST 18% x 6.34 = 1.1412
        # total = 35.4812
        r = _rupees(_model(broker="flattrade"), _trade(100.0, 100.0))
        assert r["cost_brokerage_bps"] == pytest.approx(0.0, abs=1e-9)
        assert r["modeled_cost_bps"] == pytest.approx(35.4812, abs=1e-9)

    def test_an_unlisted_broker_prices_on_the_generic_schedule(self):
        generic = _rupees(_model(broker=None), _trade(100.0, 101.0))
        other = _rupees(_model(broker="no-such-broker"), _trade(100.0, 101.0))
        assert other == generic

    def test_winner_and_loser_at_the_same_entry_pay_different_stt(self):
        # long, notional 1,00,000: exit 99 -> sell 99,000 -> STT 24.75; exit 101 -> 25.25
        m = _model()
        loser = _rupees(m, _trade(100.0, 99.0))
        winner = _rupees(m, _trade(100.0, 101.0))
        assert loser["cost_stt_bps"] == pytest.approx(24.75, abs=1e-9)
        assert winner["cost_stt_bps"] == pytest.approx(25.25, abs=1e-9)
        assert winner["modeled_cost_bps"] > loser["modeled_cost_bps"]

    def test_slippage_is_two_sides_of_the_entry_notional_not_the_exit(self):
        m = _model()
        comp = m.components(_trade(100.0, 150.0))
        assert comp["cost_slippage_bps"] == pytest.approx(8.0, abs=1e-12)
        assert m.components(_trade(100.0, 150.0), slippage_mult=2.0)[
            "cost_slippage_bps"
        ] == pytest.approx(16.0, abs=1e-12)


class TestInvariants:
    def test_components_sum_to_trade_cost_exactly(self):
        m = _model(broker="flattrade", product="CNC", leverage=1.0)
        for exit_ in (95.0, 100.0, 108.0):
            for d in (1, -1):
                t = _trade(100.0, exit_, direction=d)
                comp = m.components(t)
                parts = [comp[c] for c in costs.COMPONENT_COLUMNS]
                assert comp["modeled_cost_bps"] == m.trade_cost_bps(t)
                assert sum(parts) == pytest.approx(comp["modeled_cost_bps"], abs=1e-9)
                assert m.statutory_bps(t) + 8.0 == pytest.approx(m.trade_cost_bps(t), abs=1e-9)

    def test_repeated_runs_are_identical(self):
        rng = np.random.default_rng(7)
        trades = _random_trades(rng, 200)
        m = _model(fixed_notional=None)
        runs = [decompose_trades(trades, m).to_numpy() for _ in range(3)]
        assert np.array_equal(runs[0], runs[1])
        assert np.array_equal(runs[1], runs[2])
        again = [m.trade_cost_bps(t) for t in trades]
        assert again == [m.trade_cost_bps(t) for t in trades]

    def test_recost_restates_from_the_real_legs(self):
        m = _model(fixed_notional=50_000.0)
        t = dataclasses.replace(_trade(100.0, 180.0), r_gross=0.8, r_net=0.8, risk=1.0)
        out = recost([t], m)[0]
        bps = m.trade_cost_bps(t)
        assert out.r_net == pytest.approx(0.8 - bps / 1e4 * t.entry / t.risk, abs=1e-12)
        assert bps == pytest.approx(70.53684 / 50_000.0 * 1e4 + 8.0, abs=1e-9)

    def test_cost_model_version_is_two_and_in_the_identity(self):
        assert costs.COST_MODEL_VERSION == 2
        assert costs.cost_identity(_model())["cost_model_version"] == 2


class TestRefusals:
    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_trade_without_a_usable_exit_is_refused(self, bad):
        m = _model()
        t = _trade(100.0, bad)
        with pytest.raises(CostModelUnavailable):
            m.trade_cost_bps(t)
        with pytest.raises(CostModelUnavailable):
            m.components(t)
        with pytest.raises(CostModelUnavailable):
            decompose_trades([_trade(), t], m)
        with pytest.raises(CostModelUnavailable):
            recost([t], m)

    @pytest.mark.parametrize("bad", [0.0, -5.0, float("nan")])
    def test_a_trade_without_a_usable_entry_is_refused(self, bad):
        with pytest.raises(CostModelUnavailable):
            _model().trade_cost_bps(_trade(bad, 100.0))

    def test_the_symmetric_probe_needs_no_exit_and_equals_an_exit_at_entry(self):
        m = _model()
        probe = _trade(1.0, 0.0)
        for d in (1, -1):
            p = dataclasses.replace(probe, direction=d)
            flat = dataclasses.replace(p, exit=p.entry)
            assert m.trade_cost_bps(p, symmetric_legs=True) == m.trade_cost_bps(flat)
            assert m.components(p, symmetric_legs=True) == m.components(flat)
            assert m.statutory_bps(p, symmetric_legs=True) == m.statutory_bps(flat)

    def test_symmetric_probe_is_not_the_default(self):
        with pytest.raises(CostModelUnavailable):
            _model().trade_cost_bps(_trade(1.0, 0.0))


def _random_trades(rng, n):
    trades = []
    for i in range(n):
        entry = float(rng.uniform(50.0, 4000.0))
        move = float(rng.uniform(-0.08, 0.08))
        risk = float(entry * rng.uniform(0.003, 0.05))
        t = _trade(entry, entry * (1.0 + move), direction=int(rng.choice([1, -1])), risk=risk)
        trades.append(dataclasses.replace(t, symbol=f"S{i}"))
    return trades


def _reference_bps(model: CostModel, trade: Trade) -> float:
    """Scalar legwise reference straight from the portfolio schedule, one order per leg."""
    build = india_intraday if model.product == "MIS" else india_delivery
    schedule = build(model.exchange, broker=model.broker)
    notional = model.notional(trade)
    entry_value = notional
    exit_value = notional * trade.exit / trade.entry
    buy, sell = (entry_value, exit_value) if trade.direction != -1 else (exit_value, entry_value)
    rupees = schedule.breakdown(buy, 0.0, 1)["total"] + schedule.breakdown(0.0, sell, 1)["total"]
    if model.product == "CNC" and trade.direction != -1:
        rupees += fees_for(model.broker).dp_charge * (1.0 + schedule.tax_rate)
    return rupees / notional * 1e4 + 2.0 * model.slippage_bps_per_side


class TestVectorisedEqualsScalarReference:
    @pytest.mark.parametrize("product", ["MIS", "CNC"])
    @pytest.mark.parametrize("broker", [None, "flattrade"])
    @pytest.mark.parametrize("fixed", [None, 75_000.0])
    def test_random_trades_match_to_1e_9(self, product, broker, fixed):
        rng = np.random.default_rng(20261105)
        trades = _random_trades(rng, 300)
        m = _model(product=product, broker=broker, fixed_notional=fixed)
        frame = decompose_trades(trades, m)
        expected = np.array([_reference_bps(m, t) for t in trades])
        assert np.max(np.abs(frame["modeled_cost_bps"].to_numpy() - expected)) < 1e-9
        scalar = np.array([m.trade_cost_bps(t) for t in trades])
        assert np.max(np.abs(scalar - expected)) < 1e-9
        parts = frame[list(costs.COMPONENT_COLUMNS)].sum(axis=1).to_numpy()
        assert np.max(np.abs(parts - expected)) < 1e-9

    def test_a_flat_brokerage_schedule_is_priced_per_leg(self, monkeypatch):
        # a broker with a flat Rs 10 delivery order: two legs -> Rs 20, taxed
        fees = dataclasses.replace(fees_for("flattrade"), delivery_flat=10.0)
        from portfolio import broker_fees

        monkeypatch.setitem(broker_fees.BROKER_FEES, "flattrade", fees)
        m = _model(product="CNC", broker="flattrade", leverage=1.0)
        t = _trade(100.0, 102.0)
        r = _rupees(m, t)
        # brokerage 2 x 10 = 20 + GST on it is in the gst column; DP 23.60 in brokerage
        assert r["cost_brokerage_bps"] == pytest.approx(20.0 + 23.6, abs=1e-9)
        assert abs(m.trade_cost_bps(t) - _reference_bps(m, t)) < 1e-9
