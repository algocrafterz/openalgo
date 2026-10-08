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
    #: True: the engine asks target() for the take-profit level on every bar of an open
    #: position instead of using the fixed signal target, and a bar that opens beyond that
    #: level fills at its open. False (every strategy before IDEA-029) leaves the engine as is.
    dynamic_target: bool = False
    #: True: the strategy implements `null_entry`, so the cross-sectional selection baseline
    #: (`backtest.baseline_cross_sectional`) can replay its stop and target rules on every
    #: eligible symbol of a session. Opt-in; False records no such block.
    supports_selection_null: bool = False

    # ---- required -------------------------------------------------------

    @abstractmethod
    def prepare(self, df: pd.DataFrame, p) -> pd.DataFrame:
        """Attach every column entry() will read. Called once per symbol.

        Anything derived from a completed higher-timeframe or previous-day value MUST
        be shifted here, not in entry(). Use signal_engine.backtest.indicators.
        """

    def prepare_for(self, symbol: str, df: pd.DataFrame, p) -> pd.DataFrame:
        """Symbol-aware prepare(). The harness calls THIS; override it when the strategy
        needs the symbol (broker tick size, extra data such as 1-minute bars). The default
        ignores the symbol and calls prepare(), so existing adapters are unchanged."""
        return self.prepare(df, p)

    @abstractmethod
    def entry(self, c: Ctx, i: int, p, direction: int) -> EntrySignal | None:
        """Return a signal to fill at bar i+1's open, or None.

        Bar i is CLOSED. Never read c[...][i + 1]. The engine has already checked the
        session window, the per-day trade cap and the direction permissions.
        """

    def null_entry(self, c: Ctx, i: int, p, direction: int) -> EntrySignal | None:
        """The signal this strategy's own stop and target rules would place at closed bar i on
        ANY symbol, ignoring the selection test (the rule that picks which names trade).

        Used only by the cross-sectional selection baseline, which replays the strategy's
        exit logic on every eligible symbol of a session. Compute the stop and target from
        that symbol's own indicators. Return None when the symbol is outside the strategy's
        opportunity set (for example no gap in the direction the strategy trades).

        Contract: the strategy's per-symbol `prepare_for` must not need panel rank columns,
        because the baseline prepares one symbol at a time. Set `supports_selection_null`.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement null_entry")

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

    def target(self, c: Ctx, j: int, p, pos: Position) -> float:
        """The take-profit level on bar j, read only when `dynamic_target` is True.

        Must use data from bars <= j - 1 only: the level is a resting order placed before
        bar j trades. The stop still wins a bar that reaches both.
        """
        return pos.tp

    def on_fill(self, c: Ctx, i: int, p, pos: Position) -> None:
        """Called when a pending signal fills at bar i's open."""
        return None

    def on_fill_skipped(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        """Called when RunConfig.skip_fill_beyond_levels drops the fill at bar i's open."""
        return None

    def on_exit(self, c: Ctx, i: int, p, trade) -> None:
        """Called when the position closes on bar i, with the finished Trade."""
        return None

    def custom_exit(self, c: Ctx, i: int, p, pos: Position) -> str | None:
        """Return an exit reason to close at this bar's close, or None.

        Checked only after stop and target. Use for rules like "close back across the
        9 EMA" or "opposing crossover".
        """
        return None
