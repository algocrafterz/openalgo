"""Execution sinks: where a finished Signal goes. Modes differ only here.

RecordingSink is shadow mode: it logs the would-be signal and places nothing. DispatchSink
hands the Signal to the engine's existing pipeline (main.dispatch_signal), which is analyze
or live depending on OpenAlgo's mode. SimSink (sim_sink.py) plays the broker for replay.

on_bar and after_bar are the hooks a simulated broker needs and a real one does not: the cycle
tells the sink about each closed bar before it steps exits (fill pending orders, trigger stops)
and again after (time exit). Both are no-ops for every sink that talks to a real broker.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

import pandas as pd
from loguru import logger

from signal_engine.models import Signal


class ExecutionSink(Protocol):
    dispatches: bool  # True when submit() can place an order

    async def submit(self, signal: Signal) -> None: ...

    async def on_bar(
        self, symbol: str, bar_open: pd.Timestamp, o: float, h: float, low: float, c: float
    ) -> None: ...

    async def after_bar(
        self, symbol: str, bar_open: pd.Timestamp, o: float, h: float, low: float, c: float
    ) -> None: ...

    def has_pending(self, symbol: str) -> bool:
        """True while an order armed for the symbol has not filled yet (simulated brokers only)."""
        ...


class RecordingSink:
    dispatches = False
    #: the cycle writes this sink's signals and skips to the state store (shadow_events)
    records_shadow = True

    def __init__(self) -> None:
        self.signals: list[Signal] = []

    async def submit(self, signal: Signal) -> None:
        self.signals.append(signal)
        logger.info(
            f"shadow signal {signal.strategy} {signal.direction.value} {signal.symbol} "
            f"entry={signal.entry} sl={signal.sl} tp={signal.tp} sig_id={signal.sig_id}"
        )

    async def on_bar(self, symbol, bar_open, o, h, low, c) -> None:
        return None

    async def after_bar(self, symbol, bar_open, o, h, low, c) -> None:
        return None

    def has_pending(self, symbol) -> bool:
        return False


class DispatchSink:
    """Hands each Signal to the engine's dispatch path (validate, risk, sizing, orders)."""

    dispatches = True

    def __init__(self, dispatch: Callable[[Signal], Awaitable[None]]) -> None:
        self._dispatch = dispatch

    async def submit(self, signal: Signal) -> None:
        logger.info(
            f"dispatch {signal.strategy} {signal.direction.value} {signal.symbol} "
            f"sig_id={signal.sig_id} tp_level={signal.tp_level}"
        )
        await self._dispatch(signal)

    async def on_bar(self, symbol, bar_open, o, h, low, c) -> None:
        return None

    async def after_bar(self, symbol, bar_open, o, h, low, c) -> None:
        return None

    def has_pending(self, symbol) -> bool:
        return False
