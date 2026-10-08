"""The contract a strategy fulfils to plug into the shared pipeline.

A Strategy is a pure function from bar frames to Fires. It holds no I/O, no clock and no
order logic, so the same object serves a full-history backtest, a bar-by-bar replay, paper
and live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class Fire:
    """One signal a strategy produced, indexed by the bar that closed and triggered it."""

    symbol: str
    bar_time: pd.Timestamp  # open time of the signal bar, tz-aware IST
    direction: int  # 1 long, -1 short
    entry_ref: float  # the signal bar's close
    sl: float
    targets: tuple[float, ...]  # first entry is the primary target (TP1); empty only with context take_profit "none"
    tag: str  # setup code, e.g. "PDH-RT"
    context: dict = field(default_factory=dict)


@runtime_checkable
class Strategy(Protocol):
    name: str  # strategy tag, matches strategies.REGISTRY
    interval_min: int  # bar size the strategy runs on
    warmup_sessions: int  # prior sessions of bars the inputs need
    needs_1m: bool  # whether prior-session 1-minute bars are required
    entry_window_min: tuple[int, int]  # [start, cutoff) as minutes after midnight IST

    def evaluate(self, symbol: str, df5m: pd.DataFrame, df1m: pd.DataFrame) -> list[Fire]:
        """Every fire in the window, oldest first. Pure and causal."""
        ...
