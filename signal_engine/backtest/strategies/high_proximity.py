"""IDEA-027: proximity to the 52-week high, a weekly re-ranked long-only book.

The rules are the frozen spec's, nothing more (research/ideas/idea-027-52-week-high-proximity):

  On the last session of each ISO week, after the close, take every eligible stock's close as a
  fraction of the highest close of the trailing `lookback_sessions` calendar sessions. A stock
  qualifies when that fraction is at least 1 - closeness_pct / 100. Hold the `max_positions`
  qualifying stocks with the highest fraction; with fewer qualifiers, hold fewer and keep the rest
  in cash. Orders fill at the next session's open. A held stock that is not in the new set is sold
  at that open. No stop, no target, no maximum holding period.

The selection depends only on prices on the rebalance date (a full re-rank), so each symbol can be
simulated on its own against the schedule worked out in `prepare_panel`.

Assumptions confirmed by the owner for v1:
  - Ties at the cut are broken by a hash of symbol and date (`panel.top_n_schedule`).
  - The window is `lookback_sessions` calendar sessions of the run's derived calendar, counted from
    the symbol's first bar the engine sees, after the platform's eligibility and warm-up handling.
    The first signal therefore comes roughly 312 sessions after a symbol's first observed bar when
    its first 60 sessions are removed as ineligible. It is not 252 sessions from listing.
  - Sessions missing from a symbol's frame inside the window are skipped; no minimum coverage is
    required. What was removed is recorded in the run's exclusions and data-quality evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from signal_engine.backtest import panel
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position

SUPPORTED_REBALANCE = "weekly"
SUPPORTED_REBALANCE_DAY = "last_session_of_week"
EXIT_REASON = "REBALANCE_EXIT"


@dataclass
class HighProximityParams:
    """Exactly the parameters the frozen spec lists, with the spec's values."""

    lookback_sessions: int = 252
    closeness_pct: float = 5
    max_positions: int = 20
    rebalance: str = SUPPORTED_REBALANCE
    rebalance_day: str = SUPPORTED_REBALANCE_DAY


class HighProximity(SwingStrategy):
    name = "high-proximity"
    tag = "HIGH_PROX"

    def prepare_panel(self, frames, p: HighProximityParams, calendar):
        """The weekly held sets from every symbol's closes, attached to each symbol as columns."""
        if p.rebalance != SUPPORTED_REBALANCE or p.rebalance_day != SUPPORTED_REBALANCE_DAY:
            raise ValueError(
                f"high-proximity supports only rebalance={SUPPORTED_REBALANCE!r} on "
                f"{SUPPORTED_REBALANCE_DAY!r}, got {p.rebalance!r} on {p.rebalance_day!r}"
            )
        sessions = list(calendar.trading_days)
        close = panel.close_panel(frames, sessions)
        high, history_ok = panel.trailing_high(close, p.lookback_sessions)
        score = (close / high).where(history_ok & close.notna())
        rebalances = panel.weekly_rebalance_sessions(sessions)
        min_score = (100 - p.closeness_pct) / 100
        schedule = panel.top_n_schedule(score, rebalances, p.max_positions, min_score)
        return panel.attach_schedule(frames, rebalances, schedule, sessions)

    def prepare(self, df: pd.DataFrame, p: HighProximityParams) -> pd.DataFrame:
        out = df.copy()
        out["day"] = pd.Series(out.index.date, index=out.index)
        return out

    def entry_at_open(self, c: Ctx, i: int, p: HighProximityParams, direction: int):
        """Buy at this open when the previous session's rebalance put the stock in the held set."""
        if direction != 1 or not c["target_prev"][i]:
            return None
        return EntrySignal(direction=1, sl=None, tp=math.inf, tag=self.tag)

    def exit_at_open(self, c: Ctx, i: int, p: HighProximityParams, pos: Position):
        """Sell at this open when the latest rebalance left the stock out of the held set."""
        return None if c["target_latest"][i] else EXIT_REASON
