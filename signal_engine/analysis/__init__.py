"""Post-trade analysis: reconcile what was signalled against what the broker actually did.

`signal_engine/backtest/` answers "would this strategy have worked". This package answers
the different and harder question "what did we ACTUALLY get, and where did it leak".

The two are not interchangeable and the gap between them is the whole point. A backtest
fills at a bar's open with a modelled cost; live, the alert advertises one price, the
engine sizes and sends an order, the broker fills somewhere else, a partial books at TP1,
the remainder is squared off on a clock, and charges come off the top. Every one of those
steps loses something, and none of them are visible in a Telegram channel.
"""

from signal_engine.analysis.ledger import (
    Leg,
    Position,
    build_ledger,
    load_engine_events,
    load_fills,
)

__all__ = ["Leg", "Position", "build_ledger", "load_engine_events", "load_fills"]
