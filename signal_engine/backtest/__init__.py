"""Backtest framework for OpenAlgo's PineScript-based strategies.

Every tradeable PineScript in signal_engine/pinescripts/ should have an adapter here,
so a strategy can be measured before it is wired to a live broker.

    from signal_engine.backtest import Backtest, RunConfig, data
    from signal_engine.backtest.strategies.ema9 import Ema9, Ema9Params

    bt = Backtest(Ema9(), data.load(data.NSE_LIQUID), RunConfig(cost_bps=10))
    print(bt.confirm(Ema9Params()))

See .claude/skills/pinescript-strategy/ for the full workflow.
"""

from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.types import EntrySignal, RunConfig, Trade

__all__ = ["Backtest", "RunConfig", "EntrySignal", "Trade"]
