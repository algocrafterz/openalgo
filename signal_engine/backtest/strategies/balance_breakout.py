"""Multi-day balance breakout (IDEA-034) - backtest adapter.

The rules live in strategy_core/balance_breakout.py, shared with the forward paper recorder
(analysis/idea034_paper.py); this module only wires them to the backtest engine.

THE IDEA
    A stock whose last N completed sessions form a balance (overlapping 70 percent value
    areas, composite range narrow against the daily ATR) has agreed on value. Trade the first
    ACCEPTED close outside that composite range, after the initial balance and before 13:00.
    Every level is computed from completed prior sessions, so it is known at 09:15.

RULES (pre-registered in research/ideas/idea-034-multi-day-balance-breakout/idea.md)
    Daily profile: 5m bars of the continuous session, each bar's volume spread evenly over the
    price bins its High-Low range touches; bin = 0.05 percent of the session mean close (floored
    at the 0.05 tick); POC is the fullest bin; the value area grows from the POC one bin at a
    time toward the side whose next two bins hold more volume until 70 percent is inside.
    Balance on day t: adjacent value areas of the last N sessions overlap by at least
    `min_va_overlap` of the narrower one, and composite height H = max(VAH) - min(VAL) is at
    most `atr_max_multiple` x daily ATR14 (mean true range of the 14 sessions before t).
    Trigger "p30": a 30-minute period close (anchored 09:15, ending 10:15 .. 12:45) beyond the
    composite edge while the previous 30-minute close was not. Trigger "two5": two consecutive 5m
    closes beyond the edge (both closing at or after 10:15, the second by 13:00) with the close
    before them inside. First breakout of the day only. Fill at the next 5m open.
    Stop: "edge" = edge -/+ `stop_buffer_atr` x ATR, or "mid" = composite midpoint.
    Target: the broken edge projected by H. Time exit 14:45 (engine).

PLACEBOS (not part of the strategy; they isolate what each ingredient adds)
    "non_balance": same trigger on the same composite range when H is above the balance cap.
    "random_entry": on balance days, a random signal slot and side (seeded by symbol and day),
    with the stop and target distances the real rule would use, measured from the signal close.

TIMESTAMPS: the index is IST (add_session_columns). Bars are stamped with their open time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal
from signal_engine.strategy_core.balance_breakout import (  # noqa: F401 - re-exported
    LAST_SIGNAL_CLOSE_MIN,
    TICK,
    BalanceParams,
    balance_levels,
    daily_table,
    mark_signals,
    value_area,
)


class BalanceBreakout(Strategy):
    name = "balance_breakout - first accepted breakout of a multi-day value-area balance"
    tag = "BALBRK"
    pine = ""
    run_overrides = {
        "skip_open_minutes": 0,
        "entry_cutoff_min": LAST_SIGNAL_CLOSE_MIN,
        "max_trades_per_day": 1,
        "skip_fill_beyond_levels": True,
    }

    def __init__(self) -> None:
        self._tables: dict = {}

    def prepare_key(self, p: BalanceParams) -> tuple:
        return tuple(sorted(vars(p).items()))

    def prepare(self, df: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
        return self.prepare_for("", df, p)

    def _table(self, symbol: str, d: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
        key = (
            symbol,
            len(d),
            d.index[0],
            d.index[-1],
            p.value_area_pct,
            p.bin_pct_of_price,
            p.min_session_bars,
        )
        if symbol and key in self._tables:
            return self._tables[key]
        tab = daily_table(d, p)
        if symbol:
            self._tables = {k: v for k, v in self._tables.items() if k[0] == symbol}
            self._tables[key] = tab
        return tab

    def prepare_for(self, symbol: str, df: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        n = len(d)
        d["sig_dir"] = np.zeros(n, dtype=np.int8)
        d["sig_sl"] = np.nan
        d["sig_tp"] = np.nan
        tab = self._table(symbol, d, p)
        if len(tab) <= p.balance_days + p.atr_len:
            return d
        return mark_signals(d, tab, p, symbol)

    def entry(self, c: Ctx, i: int, p: BalanceParams, direction: int) -> EntrySignal | None:
        if int(c["sig_dir"][i]) != direction:
            return None
        sl, tp = float(c["sig_sl"][i]), float(c["sig_tp"][i])
        if not (np.isfinite(sl) and np.isfinite(tp)):
            return None
        return EntrySignal(direction=direction, sl=sl, tp=tp, tag=self.tag)
