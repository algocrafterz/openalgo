"""IDEA-028: lowest realised volatility, a monthly re-ranked long-only book.

The rules are the frozen spec's, nothing more (research/ideas/idea-028-low-volatility):

  On the last session of each calendar month that has a next session, after the close, compute
  every eligible stock's volatility: the sample standard deviation (ddof 1) of its last
  `vol_lookback_returns` daily returns r_t = Close_t / Close_(t-1) - 1, not annualised. Hold the
  `max_positions` stocks with the lowest volatility. Orders fill at the next session's open. A
  held stock that is not in the new set is sold at that open; one that stays is kept. No stop,
  no target, no maximum holding period.

Missing data (researcher decision 2026-10-05): returns are taken only between CONSECUTIVE
calendar sessions, so N returns need N + 1 consecutive closes. A stock missing any session in
that window is not ranked on that rebalance day; a missing session is never compressed into one
return that spans two sessions. A held stock that is not ranked leaves the set and is sold.

The selection depends only on prices on the rebalance date (a full re-rank), so each symbol can
be simulated on its own against the schedule worked out in `prepare_panel`. Ties are broken by
the platform's hash of symbol and date (`panel.top_n_schedule`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from signal_engine.backtest import panel
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position

SUPPORTED_REBALANCE = "monthly"
SUPPORTED_REBALANCE_DAY = "last_session_of_month"
EXIT_REASON = "REBALANCE_EXIT"


@dataclass
class LowVolatilityParams:
    """Exactly the parameters the frozen spec lists, with the spec's values."""

    vol_lookback_returns: int = 252
    max_positions: int = 30
    rebalance: str = SUPPORTED_REBALANCE
    rebalance_day: str = SUPPORTED_REBALANCE_DAY


def return_volatility(close: pd.DataFrame, n_returns: int) -> pd.DataFrame:
    """The sample standard deviation (ddof 1) of the last `n_returns` simple daily returns.

    `close` is a (calendar session x symbol) panel with NaN where a symbol has no bar. A return
    exists only between two consecutive calendar sessions that both have a close, and the value
    is NaN unless all `n_returns` returns in the window exist, so the first value needs
    `n_returns + 1` consecutive closes.
    """
    if n_returns < 2:
        raise ValueError(f"n_returns must be >= 2 for a sample standard deviation, got {n_returns}")
    returns = close / close.shift(1) - 1
    return returns.rolling(n_returns, min_periods=n_returns).std(ddof=1)


class LowVolatility(SwingStrategy):
    name = "low-volatility"
    tag = "LOW_VOL"

    def prepare_panel(self, frames, p: LowVolatilityParams, calendar):
        """The monthly held sets from every symbol's closes, attached to each symbol as columns."""
        if p.rebalance != SUPPORTED_REBALANCE or p.rebalance_day != SUPPORTED_REBALANCE_DAY:
            raise ValueError(
                f"low-volatility supports only rebalance={SUPPORTED_REBALANCE!r} on "
                f"{SUPPORTED_REBALANCE_DAY!r}, got {p.rebalance!r} on {p.rebalance_day!r}"
            )
        sessions = list(calendar.trading_days)
        close = panel.close_panel(frames, sessions)
        volatility = return_volatility(close, p.vol_lookback_returns)
        rebalances = panel.monthly_rebalance_sessions(sessions)
        # top_n_schedule keeps the highest scores, so the lowest volatility ranks first.
        schedule = panel.top_n_schedule(-volatility, rebalances, p.max_positions, -math.inf)
        return panel.attach_schedule(frames, rebalances, schedule, sessions)

    def prepare(self, df: pd.DataFrame, p: LowVolatilityParams) -> pd.DataFrame:
        out = df.copy()
        out["day"] = pd.Series(out.index.date, index=out.index)
        return out

    def entry_at_open(self, c: Ctx, i: int, p: LowVolatilityParams, direction: int):
        """Buy at this open when the previous session's rebalance put the stock in the held set."""
        if direction != 1 or not c["target_prev"][i]:
            return None
        return EntrySignal(direction=1, sl=None, tp=math.inf, tag=self.tag)

    def exit_at_open(self, c: Ctx, i: int, p: LowVolatilityParams, pos: Position):
        """Sell at this open when the latest rebalance left the stock out of the held set."""
        return None if c["target_latest"][i] else EXIT_REASON
