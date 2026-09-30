"""Per-trade India cost model at the deployed notional (acceptance criteria G1.1, G1.3).

The engines charge one flat round-trip figure in basis points (`types.india_intraday_bps`
for intraday, a constant for swing). That hides two things the criteria ask for:

  * Brokerage is a flat Rs per order (capped at 0.03 percent), so the cost in bps depends on
    how large the position actually is. Live sizing is risk based, so a wide stop means a
    small position and a higher cost in bps; a tight stop means a large, cheap one.
  * Slippage is the uncertain part of cost and statutory charges are exact, so G1.3 stresses
    only slippage (doubled) and leaves the statutory schedule alone.

`CostModel` sizes each trade the way the live pipeline does (risk_per_trade x capital over
the stop distance, capped by leverage), prices it from the same `portfolio.costs` schedules
the Portfolio Backtester uses, and `recost` restates `r_net` from the trade's own
`r_gross`. The flat-bps engines are left as they are so stored records stay comparable;
this model is reported beside them.

Deployed capital and sizing come from the caller (record_run reads `config.yaml`), never
from a default here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

import numpy as np

from portfolio.costs import india_delivery, india_intraday
from signal_engine.backtest.types import Trade

PRODUCTS = ("MIS", "CNC")
MODEL_NAME = "statutory-per-trade"
#: G1.3 asks for total cost at these multiples of slippage.
STRESS_LEVELS = (1.0, 1.5, 2.0)
#: Positions are priced to the nearest rupee bucket; brokerage is the only lumpy line.
_NOTIONAL_BUCKET = 100.0


@lru_cache(maxsize=8192)
def _statutory_bps(product: str, exchange: str, bucket: float) -> float:
    schedule = india_intraday(exchange) if product == "MIS" else india_delivery(exchange)
    notional = bucket * _NOTIONAL_BUCKET
    charge = schedule.charge(buy_value=notional, sell_value=notional, orders=2)
    return float(charge) / notional * 1e4


@dataclass(frozen=True)
class CostModel:
    """Sizing and charge assumptions for one deployment. No field has a default."""

    capital: float
    risk_per_trade: float
    leverage: float
    product: str
    exchange: str
    slippage_bps_per_side: float

    def __post_init__(self) -> None:
        if self.product not in PRODUCTS:
            raise ValueError(f"product must be one of {PRODUCTS}, got {self.product!r}")
        for name in ("capital", "risk_per_trade", "leverage"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if self.slippage_bps_per_side < 0:
            raise ValueError("slippage_bps_per_side must not be negative")

    def notional(self, trade: Trade) -> float:
        """Rupees of stock the live sizer would have bought for this trade."""
        stop_fraction = trade.risk / trade.entry
        sized = self.capital * self.risk_per_trade / stop_fraction
        return float(min(sized, self.capital * self.leverage))

    def statutory_bps(self, trade: Trade) -> float:
        bucket = max(1.0, round(self.notional(trade) / _NOTIONAL_BUCKET))
        return _statutory_bps(self.product, self.exchange, bucket)

    def trade_cost_bps(self, trade: Trade, slippage_mult: float = 1.0) -> float:
        """Round-trip cost in bps of the entry notional: statutory plus two slippage legs."""
        return self.statutory_bps(trade) + 2.0 * self.slippage_bps_per_side * slippage_mult


def recost(trades, model: CostModel, slippage_mult: float = 1.0) -> list[Trade]:
    """Copies of `trades` with `r_net` restated under `model`. Inputs are not modified."""
    out = []
    for t in trades:
        bps = model.trade_cost_bps(t, slippage_mult)
        out.append(replace(t, r_net=t.r_gross - bps / 1e4 * t.entry / t.risk))
    return out


def cost_profile(trades, model: CostModel, flat_bps: float) -> dict[str, Any] | None:
    """The optional `cost_model` record block, or None for an empty trade list."""
    trades = list(trades)
    if not trades:
        return None
    statutory = np.array([model.statutory_bps(t) for t in trades])
    slippage = 2.0 * model.slippage_bps_per_side
    net = np.array([t.r_net for t in recost(trades, model)])
    stressed = np.array([t.r_net for t in recost(trades, model, slippage_mult=2.0)])
    return {
        "model": MODEL_NAME,
        "product": model.product,
        "exchange": model.exchange,
        "capital": model.capital,
        "risk_per_trade": model.risk_per_trade,
        "leverage": model.leverage,
        "slippage_bps_per_side": model.slippage_bps_per_side,
        "flat_bps": float(flat_bps),
        "mean_statutory_bps": round(float(statutory.mean()), 3),
        "mean_total_bps": round(float(statutory.mean()) + slippage, 3),
        "total_bps_at": {
            f"{m:.1f}": round(float(statutory.mean()) + slippage * m, 3) for m in STRESS_LEVELS
        },
        "net_r": round(float(net.mean()), 4),
        "net_r_slip_2x": round(float(stressed.mean()), 4),
        "trades": len(trades),
    }
