"""Backtest framework for OpenAlgo's PineScript-based strategies.

Every tradeable PineScript in signal_engine/strategy_library/ should have an adapter here,
so a strategy can be measured before it is wired to a live broker.

    from signal_engine.backtest import Backtest, RunConfig, data
    from signal_engine.backtest.strategies.ema9 import Ema9, Ema9Params

    bt = Backtest(Ema9(), data.load(data.NSE_LIQUID), RunConfig(cost_bps=10))
    print(bt.confirm(Ema9Params()))

See .claude/skills/pinescript-strategy/ for the full workflow.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from signal_engine.backtest.harness import Backtest
    from signal_engine.backtest.types import EntrySignal, RunConfig, Trade

__all__ = ["Backtest", "RunConfig", "EntrySignal", "Trade"]

#: Public name -> the module that defines it. Loaded on first access (PEP 562) so importing a
#: light submodule such as `compare` does not pull in the harness, pandas data loaders or duckdb.
_LAZY = {
    "Backtest": "signal_engine.backtest.harness",
    "RunConfig": "signal_engine.backtest.types",
    "EntrySignal": "signal_engine.backtest.types",
    "Trade": "signal_engine.backtest.types",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value
