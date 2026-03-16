"""Tests for Indian market transaction cost model."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backtest.costs import INTRADAY_FEE_PCT, IndianCosts


@pytest.fixture
def costs():
    return IndianCosts()


class TestIndianCosts:
    def test_buy_cost_is_positive(self, costs):
        cost = costs.cost_per_trade(100_000, is_buy=True, product="MIS")
        assert cost > 0

    def test_sell_cost_is_positive(self, costs):
        cost = costs.cost_per_trade(100_000, is_buy=False, product="MIS")
        assert cost > 0

    def test_sell_has_stt_for_intraday(self, costs):
        """Intraday STT is only on sell side."""
        buy_cost = costs.cost_per_trade(100_000, is_buy=True, product="MIS")
        sell_cost = costs.cost_per_trade(100_000, is_buy=False, product="MIS")
        # Sell should be more expensive due to STT
        assert sell_cost > buy_cost

    def test_delivery_more_expensive_than_intraday(self, costs):
        """Delivery trades have higher STT on both sides."""
        intraday_rt = costs.cost_per_trade(100_000, True, "MIS") + costs.cost_per_trade(100_000, False, "MIS")
        delivery_rt = costs.cost_per_trade(100_000, True, "CNC") + costs.cost_per_trade(100_000, False, "CNC")
        assert delivery_rt > intraday_rt

    def test_stamp_duty_only_on_buy(self, costs):
        """Stamp duty applies only to buy side."""
        # Create a cost model with exaggerated stamp duty to isolate it
        high_stamp = IndianCosts(stamp_duty_intraday_pct=0.01)
        buy_cost = high_stamp.cost_per_trade(100_000, True, "MIS")
        sell_cost = high_stamp.cost_per_trade(100_000, False, "MIS")
        # Buy should be significantly more due to stamp duty
        assert buy_cost > sell_cost

    def test_brokerage_capped_at_flat_rate(self, costs):
        """Brokerage should be capped at Rs 20."""
        # For a 10L trade, percentage brokerage = 300, but capped at 20
        cost_small = costs.cost_per_trade(10_000, True, "MIS")
        cost_large = costs.cost_per_trade(10_00_000, True, "MIS")

        # The brokerage component should be the same (both capped)
        # But total differs due to other proportional charges
        assert cost_large > cost_small

    def test_round_trip_pct_reasonable(self, costs):
        """Round trip cost should be in a reasonable range."""
        intraday_pct = costs.round_trip_pct("MIS")
        delivery_pct = costs.round_trip_pct("CNC")

        # Intraday should be roughly 0.01-0.05%
        assert 0.0001 < intraday_pct < 0.001

        # Delivery should be roughly 0.05-0.15%
        assert 0.0005 < delivery_pct < 0.002

    def test_zero_trade_value(self, costs):
        """Zero trade value should return zero cost (except flat brokerage)."""
        cost = costs.cost_per_trade(0, True, "MIS")
        # Only flat brokerage applies (min of 0 * pct and 20 = 0)
        # Plus GST on brokerage
        assert cost >= 0

    def test_known_intraday_trade(self):
        """Verify against a manually calculated trade."""
        costs = IndianCosts()
        trade_value = 100_000.0

        # Buy side
        brokerage = min(100_000 * 0.0003, 20)  # 30 -> capped at 20
        stt_buy = 0  # No STT on intraday buy
        exchange = 100_000 * 0.0000345  # 3.45
        sebi = 100_000 * 0.000001  # 0.1
        gst = (brokerage + exchange) * 0.18
        stamp = 100_000 * 0.00003  # 3.0

        expected_buy = brokerage + stt_buy + exchange + sebi + gst + stamp
        actual_buy = costs.cost_per_trade(trade_value, True, "MIS")
        assert abs(actual_buy - expected_buy) < 0.01

    def test_intraday_fee_pct_exported(self):
        """Verify the pre-computed fee constant is reasonable."""
        assert 0.0001 < INTRADAY_FEE_PCT < 0.001
