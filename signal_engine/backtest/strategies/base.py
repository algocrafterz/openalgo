"""The contract a strategy adapter implements to become backtestable.

One adapter per tradeable PineScript. The adapter's job is to answer "would this bar
produce an entry, and with what stop and target" - the engine owns everything else:
sessions, trade caps, next-bar fills, stop/target resolution, costs and bookkeeping.

STATE: an adapter may hold mutable state (a breakout awaiting its pullback, for
instance). The engine runs one symbol at a time and calls reset_symbol() before each
and reset_session() on every new trading day, so instance state is safe. Do not share
one adapter across threads.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from signal_engine.backtest.types import Ctx, EntrySignal, Position


class Strategy(ABC):
    #: Human name, used in reports.
    name: str = "unnamed"
    #: signal_engine strategy tag, must match strategies.py and the PineScript alerts.
    tag: str = ""
    #: Path to the PineScript this mirrors, so a reader can diff the two.
    pine: str = ""
    #: RunConfig fields this strategy needs changed from the intraday defaults, applied
    #: by the CLI. A setup that only arms after 10:00 measured through an 11:00 entry
    #: cutoff is being reported on a fraction of its own signals, which reads as a weak
    #: strategy rather than as a misconfigured harness. Leave empty to take the defaults.
    run_overrides: dict = {}

    # ---- required -------------------------------------------------------

    @abstractmethod
    def prepare(self, df: pd.DataFrame, p) -> pd.DataFrame:
        """Attach every column entry() will read. Called once per symbol.

        Anything derived from a completed higher-timeframe or previous-day value MUST
        be shifted here, not in entry(). Use signal_engine.backtest.indicators.
        """

    @abstractmethod
    def entry(self, c: Ctx, i: int, p, direction: int) -> EntrySignal | None:
        """Return a signal to fill at bar i+1's open, or None.

        Bar i is CLOSED. Never read c[...][i + 1]. The engine has already checked the
        session window, the per-day trade cap and the direction permissions.
        """

    def prepare_key(self, p) -> tuple:
        """Cache key for prepare(). Override to list ONLY the fields prepare() reads.

        The harness re-prepares a symbol whenever this key changes, so a narrow key
        makes an ablation over filter toggles far cheaper. Getting it wrong silently
        reuses stale indicator columns, so list every length and lookback.
        """
        return tuple(sorted(vars(p).items())) if hasattr(p, "__dict__") else (p,)

    # ---- optional hooks -------------------------------------------------

    def reset_symbol(self, p) -> None:
        """Clear state before a new symbol."""

    def reset_session(self, p) -> None:
        """Clear state at the start of each trading day."""

    def on_bar(self, c: Ctx, i: int, p) -> None:
        """Per-bar state machine, run after position management and before entry().

        Where a multi-step setup lives - e.g. "level broke, now wait for the pullback".
        """

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        """Called when a signal is accepted. Consume the setup here so it cannot re-arm."""

    def trail(self, c: Ctx, i: int, p, pos: Position) -> float | None:
        """Return a new stop level, or None to leave it. The engine only ever ratchets."""
        return None

    def custom_exit(self, c: Ctx, i: int, p, pos: Position) -> str | None:
        """Return an exit reason to close at this bar's close, or None.

        Checked only after stop and target. Use for rules like "close back across the
        9 EMA" or "opposing crossover".
        """
        return None
