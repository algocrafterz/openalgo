"""The contract a multi-day (daily-bar) strategy adapter implements.

Differs from the intraday `Strategy` base in one place that matters: the entry hook is
`entry_at_open`, not `entry`. A gap strategy is armed by the previous CLOSED bar and
triggered by today's OPEN, so the adapter is allowed to read `c["Open"][i]` - and
nothing else from bar i. Reading High, Low or Close at i is lookahead and will invent
an edge that does not exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from signal_engine.backtest.types import Ctx, EntrySignal, Position


class SwingStrategy(ABC):
    name: str = "unnamed"
    tag: str = ""
    pine: str = ""

    @abstractmethod
    def prepare(self, df: pd.DataFrame, p) -> pd.DataFrame:
        """Attach every column entry_at_open() will read. Called once per symbol.

        Anything that must be known BEFORE today's open has to be shifted here.
        """

    @abstractmethod
    def entry_at_open(self, c: Ctx, i: int, p, direction: int) -> EntrySignal | None:
        """Return a signal to fill at bar i's OPEN, or None.

        May read c["Open"][i] and any column at i-1 or earlier. Never c["High"][i],
        c["Low"][i] or c["Close"][i].
        """

    def prepare_key(self, p) -> tuple:
        return tuple(sorted(vars(p).items())) if hasattr(p, "__dict__") else (p,)

    def reset_symbol(self, p) -> None: ...
    def on_bar(self, c: Ctx, i: int, p) -> None: ...
    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None: ...

    def trail(self, c: Ctx, i: int, p, pos: Position) -> float | None:
        return None

    def custom_exit(self, c: Ctx, i: int, p, pos: Position) -> str | None:
        """Exit reason to close at THIS bar's close, or None. Checked after stop/target."""
        return None
