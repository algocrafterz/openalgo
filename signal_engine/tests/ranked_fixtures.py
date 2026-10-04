"""A test-only ranked strategy and panel. It is not registered as a research strategy.

Weekly rebalance on the last session of the ISO week; score = close / highest close over the
trailing `window` calendar sessions; hold the `n` best names scoring at least `closeness`;
sell at the next open any held name that drops out; no stop; no take profit.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd

from signal_engine.backtest import data, panel
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import EntrySignal


@dataclasses.dataclass
class RankParams:
    n: int = 3
    closeness: float = 0.9
    window: int = 20


class RankedTop(SwingStrategy):
    name = "ranked-top-test"
    tag = "RANK"
    run_overrides = {"max_hold_bars": None}

    def prepare_panel(self, frames, p, calendar):
        sessions = list(calendar.trading_days)
        close = panel.close_panel(frames, sessions)
        high, ok = panel.trailing_high(close, p.window)
        score = (close / high).where(ok & close.notna())
        rebalances = panel.weekly_rebalance_sessions(sessions)
        schedule = panel.top_n_schedule(score, rebalances, p.n, p.closeness)
        return panel.attach_schedule(frames, rebalances, schedule, sessions)

    def prepare(self, df, p):
        out = df.copy()
        out["day"] = pd.Series(out.index.date, index=out.index)
        return out

    def entry_at_open(self, c, i, p, direction):
        if direction != 1 or not c["target_prev"][i]:
            return None
        return EntrySignal(1, None, math.inf, "RANK")

    def exit_at_open(self, c, i, p, pos):
        return None if c["target_latest"][i] else "REBALANCE_EXIT"


def ranked_frames(n_sym=12, n=130, seed=11, start="2026-01-05"):
    """Random-walk daily bars with a few strongly trending names, so some weeks have qualifiers."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, tz="Asia/Kolkata")
    out = {}
    for k in range(n_sym):
        drift = 0.004 if k % 3 == 0 else (-0.002 if k % 3 == 1 else 0.0005)
        r = rng.normal(drift, 0.012, n)
        close = 100 * np.exp(np.cumsum(r))
        op = close * (1 + rng.normal(0, 0.004, n))
        hi = np.maximum(op, close) * 1.002
        lo = np.minimum(op, close) * 0.998
        raw = pd.DataFrame(
            {"Open": op, "High": hi, "Low": lo, "Close": close, "Volume": 10_000.0}, index=idx
        )
        out[f"S{k:02d}"] = data._downcast(data._split_adjust_daily(raw))
    return out
