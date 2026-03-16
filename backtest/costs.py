"""
Indian market transaction cost model.

Models realistic trading costs for NSE/BSE including:
- Brokerage (flat or percentage)
- STT (Securities Transaction Tax) - asymmetric buy/sell
- Exchange transaction charges
- SEBI turnover fee
- GST on brokerage + exchange charges
- Stamp duty (buy-side only)
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IndianCosts:
    """
    Transaction cost model for Indian equity markets.

    Default values are for discount brokers (Zerodha-style).
    All percentages are expressed as fractions (0.01 = 1%).
    """

    # Brokerage
    brokerage_pct: float = 0.0003  # 0.03% or Rs 20 flat - whichever is lower
    flat_brokerage: float = 20.0   # flat per-order brokerage cap

    # STT - Securities Transaction Tax
    stt_delivery_sell_pct: float = 0.001     # 0.1% on sell side (delivery/CNC)
    stt_delivery_buy_pct: float = 0.001      # 0.1% on buy side (delivery/CNC)
    stt_intraday_sell_pct: float = 0.00025   # 0.025% on sell side only (MIS)
    stt_intraday_buy_pct: float = 0.0        # 0% on buy side (MIS)

    # Exchange transaction charges (NSE)
    exchange_charge_pct: float = 0.0000345   # 0.00345%

    # SEBI turnover fee
    sebi_fee_pct: float = 0.000001           # 0.0001%

    # GST (18% on brokerage + exchange charges)
    gst_pct: float = 0.18

    # Stamp duty (buy-side only)
    stamp_duty_delivery_pct: float = 0.00015  # 0.015% (CNC)
    stamp_duty_intraday_pct: float = 0.00003  # 0.003% (MIS)

    def cost_per_trade(
        self,
        trade_value: float,
        is_buy: bool,
        product: str = "MIS",
    ) -> float:
        """
        Calculate total transaction cost for a single trade leg.

        Args:
            trade_value: Absolute value of the trade (price * quantity)
            is_buy: True for buy, False for sell
            product: "MIS" (intraday) or "CNC" (delivery)

        Returns:
            Total cost in INR for this leg.
        """
        is_intraday = product.upper() == "MIS"

        # Brokerage (capped at flat rate)
        brokerage = min(trade_value * self.brokerage_pct, self.flat_brokerage)

        # STT
        if is_intraday:
            stt = trade_value * (self.stt_intraday_buy_pct if is_buy else self.stt_intraday_sell_pct)
        else:
            stt = trade_value * (self.stt_delivery_buy_pct if is_buy else self.stt_delivery_sell_pct)

        # Exchange charges
        exchange = trade_value * self.exchange_charge_pct

        # SEBI fee
        sebi = trade_value * self.sebi_fee_pct

        # GST on brokerage + exchange charges
        gst = (brokerage + exchange) * self.gst_pct

        # Stamp duty (buy-side only)
        if is_buy:
            stamp = trade_value * (
                self.stamp_duty_delivery_pct if not is_intraday else self.stamp_duty_intraday_pct
            )
        else:
            stamp = 0.0

        return brokerage + stt + exchange + sebi + gst + stamp

    def round_trip_pct(self, product: str = "MIS") -> float:
        """
        Estimate round-trip cost as a percentage of trade value.

        This is a simplified estimate for VectorBT's `fees` parameter.
        VectorBT applies this symmetrically to both entry and exit.

        Returns:
            Approximate one-way fee percentage (applied to both entry and exit).
        """
        # Simulate a 100,000 INR trade round trip
        test_value = 100_000.0
        buy_cost = self.cost_per_trade(test_value, is_buy=True, product=product)
        sell_cost = self.cost_per_trade(test_value, is_buy=False, product=product)
        total_cost = buy_cost + sell_cost

        # VectorBT applies fees to both entry and exit,
        # so divide total round-trip by 2 * trade_value
        one_way_pct = total_cost / (2 * test_value)
        return one_way_pct


# Pre-built cost models
INTRADAY_COSTS = IndianCosts()  # Default is intraday/MIS
DELIVERY_COSTS = IndianCosts()  # Same model, use product="CNC" when calling

# Quick reference: approximate one-way fees for VectorBT
INTRADAY_FEE_PCT = INTRADAY_COSTS.round_trip_pct("MIS")    # ~0.011%
DELIVERY_FEE_PCT = DELIVERY_COSTS.round_trip_pct("CNC")     # ~0.056%
