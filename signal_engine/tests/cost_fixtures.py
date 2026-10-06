"""Statutory cost basis for tests that fake an `Execution` and record it.

A recorded run carries the statutory-per-trade basis (see `record_run.statutory_execution`).
Fixtures that build an `Execution` by hand price their trades through the same helper, so the
record they produce is on the same basis as a real run.
"""

from __future__ import annotations

from typing import Any

from signal_engine.backtest import costs, record_run

#: A deployed book large enough that a risk-sized trade is not clipped by leverage.
MODEL_DEFAULTS: dict[str, Any] = {
    "capital": 1_000_000.0,
    "risk_per_trade": 0.01,
    "leverage": 5.0,
    "product": "MIS",
    "exchange": "NSE",
    "slippage_bps_per_side": 4.0,
    "broker": "flattrade",
}


def make_model(**overrides: Any) -> costs.CostModel:
    return costs.CostModel(**{**MODEL_DEFAULTS, **overrides})


def statutory(
    trades, flat_bps: float = 16.0, reference_bps=(), model=None, **overrides: Any
) -> dict[str, Any]:
    """`Execution` keyword arguments (trades, costs, cost_model, cost_components) for `trades`.

    Priced with `model` when given, else with `make_model(**overrides)`.
    """
    model = model or make_model(**overrides)
    restated, block = record_run.statutory_execution(trades, model, flat_bps, reference_bps)
    return {
        "trades": restated,
        "costs": block,
        "cost_model": model,
        "cost_components": costs.decompose_trades(restated, model),
    }
