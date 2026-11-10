"""Per-leg brokerage cap in portfolio.costs.Charge.amount (basis 'order', orders == 2).

A round trip is one buy and one sell, so the per-order cap applies to each leg on its own.
The old code averaged the legs and applied the cap to the average, which overstated
brokerage when the legs straddle the cap (Rs 66,666.67 at 0.03% / Rs 20).

Run with: PYTHONPATH=. uv run --no-sync pytest test/test_portfolio_costs_leg_cap.py -q -p no:cacheprovider
"""

import random

import pytest

from portfolio.costs import Charge, CostSchedule, india_delivery, india_intraday

RATE = 0.0003
CAP = 20.0


def _old_averaged(buy, sell, orders=2):
    """The pre-fix formula, kept here only to bound the new result."""
    per_order = (buy + sell) / orders
    return min(per_order * RATE, CAP) * orders


def _brokerage(schedule, buy, sell, orders):
    return schedule.breakdown(buy, sell, orders)["brokerage"]


class TestStraddlingLegs:
    def test_unequal_legs_straddle_the_cap(self):
        s = india_intraday("NSE")
        # buy 50,000 x 0.03% = 15.00 (below cap); sell 90,000 x 0.03% = 27.00 -> cap 20.00
        # brokerage = 15.00 + 20.00 = 35.00 (old averaged: min(21, 20) x 2 = 40.00)
        b = s.breakdown(50_000.0, 90_000.0, 2)
        assert b["brokerage"] == pytest.approx(35.0, abs=1e-9)
        # STT 0.025% x 90,000 sell = 22.50
        # exchange 0.00307% x 140,000 = 4.298 ; SEBI 1e-6 x 140,000 = 0.14
        # stamp 0.003% x 50,000 buy = 1.50
        # GST 18% x (35 + 4.298 + 0.14) = 7.09884
        # total = 35 + 22.5 + 4.298 + 0.14 + 1.5 + 7.09884 = 70.53684
        assert b["total"] == pytest.approx(70.53684, abs=1e-9)
        assert round(b["total"], 2) == 70.54

    def test_swapped_legs_give_the_same_total(self):
        # A short sells at entry and buys at exit: legs swap sides. STT is on the sell
        # leg and stamp on the buy leg, so use equal rates by checking brokerage only,
        # then the full total with the swap computed by hand.
        s = india_intraday("NSE")
        assert _brokerage(s, 90_000.0, 50_000.0, 2) == pytest.approx(35.0, abs=1e-9)
        # buy 90,000 / sell 50,000: brokerage 35.00; STT 0.025% x 50,000 = 12.50;
        # exchange 4.298; SEBI 0.14; stamp 0.003% x 90,000 = 2.70; GST 7.09884
        # total = 35 + 12.5 + 4.298 + 0.14 + 2.7 + 7.09884 = 61.73684
        assert s.charge(90_000.0, 50_000.0, 2) == pytest.approx(61.73684, abs=1e-9)

    def test_both_legs_below_the_cap_unchanged(self):
        s = india_intraday("NSE")
        # 30,000 x 0.03% = 9.00 ; 50,000 x 0.03% = 15.00 -> 24.00 (same as averaged)
        assert _brokerage(s, 30_000.0, 50_000.0, 2) == pytest.approx(24.0, abs=1e-9)
        assert _old_averaged(30_000.0, 50_000.0) == pytest.approx(24.0, abs=1e-9)

    def test_both_legs_above_the_cap_is_two_caps(self):
        s = india_intraday("NSE")
        assert _brokerage(s, 100_000.0, 300_000.0, 2) == pytest.approx(40.0, abs=1e-9)

    def test_equal_legs_unchanged(self):
        s = india_intraday("NSE")
        assert _brokerage(s, 100_000.0, 100_000.0, 2) == pytest.approx(40.0, abs=1e-9)
        assert _brokerage(s, 50_000.0, 50_000.0, 2) == pytest.approx(30.0, abs=1e-9)


class TestUnchangedPaths:
    def test_zero_sided_leg_stays_on_the_old_path(self):
        s = india_intraday("NSE")
        # orders=2 with sell 0: old path, per_order = 45,000 -> 13.50 each -> 27.00
        assert _brokerage(s, 90_000.0, 0.0, 2) == pytest.approx(27.0, abs=1e-9)
        assert _brokerage(s, 0.0, 90_000.0, 2) == pytest.approx(27.0, abs=1e-9)

    def test_flat_per_order_unchanged(self):
        s = CostSchedule(
            name="flat",
            charges=(Charge(key="brokerage", label="B", basis="order", flat=20.0, cap=20.0),),
        )
        assert _brokerage(s, 50_000.0, 90_000.0, 2) == pytest.approx(40.0, abs=1e-9)

    def test_zero_brokerage_unchanged(self):
        s = india_intraday("NSE", broker="flattrade")
        assert _brokerage(s, 50_000.0, 90_000.0, 2) == 0.0

    def test_rate_without_cap_is_linear(self):
        c = Charge(key="b", label="B", basis="order", rate=RATE)
        assert c.amount(50_000.0, 90_000.0, 2) == pytest.approx(42.0, abs=1e-9)

    def test_delivery_default_unchanged(self):
        s = india_delivery("NSE")
        assert _brokerage(s, 50_000.0, 90_000.0, 2) == 0.0

    def test_delivery_calculator_check_still_holds(self):
        # Buy and sell 5,000 shares at 1,000 on NSE: the public calculator figure.
        s = india_delivery("NSE")
        assert s.charge(5_000_000.0, 5_000_000.0, 0) == pytest.approx(11_124.06, abs=0.005)
        assert s.charge(5_000_000.0, 5_000_000.0, 2) == pytest.approx(11_124.06, abs=0.005)

    def test_single_order_is_its_own_value(self):
        s = india_intraday("NSE")
        # 90,000 x 0.03% = 27.00 -> capped at 20.00
        assert _brokerage(s, 90_000.0, 0.0, 1) == pytest.approx(20.0, abs=1e-9)

    def test_n_orders_keep_the_averaged_approximation(self):
        s = india_intraday("NSE")
        # 4 orders, 400,000 total: average 100,000 -> 30 -> cap 20 each -> 80.00
        assert _brokerage(s, 200_000.0, 200_000.0, 4) == pytest.approx(80.0, abs=1e-9)
        # 3 orders, 50,000 + 90,000 + 10,000 style aggregate: average 80,000 -> cap x 3
        assert _brokerage(s, 120_000.0, 120_000.0, 3) == pytest.approx(60.0, abs=1e-9)
        # 4 orders at 40,000 average -> 12.00 each, not capped -> 48.00
        assert _brokerage(s, 80_000.0, 80_000.0, 4) == pytest.approx(48.0, abs=1e-9)

    def test_no_orders_is_zero(self):
        assert _brokerage(india_intraday("NSE"), 50_000.0, 90_000.0, 0) == 0.0


class TestProperty:
    def test_two_order_result_equals_two_single_leg_calls(self):
        rng = random.Random(20261107)
        c = Charge(key="b", label="B", basis="order", rate=RATE, cap=CAP)
        for _ in range(2000):
            buy = rng.uniform(1.0, 500_000.0)
            sell = rng.uniform(1.0, 500_000.0)
            both = c.amount(buy, sell, 2)
            legs = c.amount(buy, 0.0, 1) + c.amount(0.0, sell, 1)
            assert both == pytest.approx(legs, abs=1e-9)
            assert both <= _old_averaged(buy, sell) + 1e-9
            assert both == pytest.approx(
                min(buy * RATE, CAP) + min(sell * RATE, CAP), abs=1e-9
            )
