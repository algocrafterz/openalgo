"""The contract a multi-day (daily-bar) strategy adapter implements.

Differs from the intraday `Strategy` base in one place that matters: the entry hook is
`entry_at_open`, not `entry`. A gap strategy is armed by the previous CLOSED bar and
triggered by today's OPEN, so the adapter is allowed to read `c["Open"][i]` - and
nothing else from bar i. Reading High, Low or Close at i is lookahead and will invent
an edge that does not exist.

Two optional hooks are found by name, so a strategy that does not define them is unaffected:

  prepare_panel(frames, params, calendar) -> frames
      Called once, before the symbol-by-symbol run, with every symbol's frame and the run's
      derived calendar. A strategy that ranks the universe computes its cross-sectional decision
      here (see `backtest/panel.py`) and hands it to each symbol as columns.
  exit_at_open(c, i, p, pos) -> str | None
      Asked at bar i's open for an open position, never on its entry bar. A reason closes the
      position at that open; it may read bar i's Open and anything at i-1 or earlier. The same
      symbol is not bought again on that bar.

`EntrySignal.sl` may be None for a strategy with no stop. Risk and R are then undefined and the
trade carries its result as a return on the entry price.
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

    def prepare_for(self, symbol: str, df: pd.DataFrame, p) -> pd.DataFrame:
        """Symbol-aware prepare(). The harness calls THIS, as it does for the intraday
        base; the default ignores the symbol and calls prepare()."""
        return self.prepare(df, p)

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
