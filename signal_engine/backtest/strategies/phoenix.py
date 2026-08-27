"""Phoenix Bird rebound swing - backtest adapter.

Source: "Phoenix Swing Trading Strategy Backtest Results", FabTrader, 2026-05-05
(fabtrader.in/videos/phoenix-swing-trading-strategy-backtest-results) and the
companion article fabtrader.in/blog/the-phoenix-bird-swing-trading-strategy-backtested,
which ships the full Python source. Rules below are quoted from that source.

THE IDEA
    A stock is knocked down hard by an overreaction, then stops getting worse. Buy the
    turn, not the fall. The drop is measured by a 14-day rate of change; the turn is
    "ROC is improving" against two earlier readings.

      ROC14 < -20            severe drop (the article's own code loosens this to -15
                             "too few trades for -20 ROC limit")
      ROC14 > ROC14[1]       downside pressure easing
      ROC14 > ROC14[3]       and easing for more than one day
      -> buy the NEXT day's open, long only

    Exit: target = entry + 1.0 x ATR14, stop = entry - 2.5 x ATR14 (both frozen at
    entry), time stop after 10 days.

WHY THIS NEEDS RE-TESTING RATHER THAN TRUSTING THE PUBLISHED RESULT
    The published sample is Jan-Mar 2025, FIVE trades, 80% win rate, PF ~4. Two
    problems, both fatal to that number:

    1. THE PAYOFF DEMANDS THE WIN RATE.  1 ATR target against a 2.5 ATR stop needs
       2.5/3.5 = 71.4% wins just to break even BEFORE costs. The whole strategy is a
       bet that the win rate clears 71.4%, and five trades cannot establish that.

    2. THE SOURCE BACKTEST LEAKS POSITIONS BETWEEN SYMBOLS.  `in_position`,
       `entry_price`, `target` and `stop_loss` are initialised OUTSIDE the ticker loop,
       so a position still open when one symbol's data ends carries into the next symbol
       and is then closed against that symbol's prices.

    Its loop also checks `if high >= target` BEFORE `elif low <= stop_loss`, booking the
    target on any bar that spans both. That one turns out NOT to matter here - measured
    over 715 trades, exactly one bar spanned both levels, because 1 ATR + 2.5 ATR is a
    3.5 ATR range and a single daily bar rarely covers it. `swing_engine` takes the stop
    first regardless, which is the correct default.

WHAT THE RE-TEST FOUND (201 F&O names, 2016-2026, 20 bps round trip)
    The edge is real but tiny. ROC < -20 gives n=320, 67% wins, +0.087 R/trade, t=3.6,
    and it survives dropping every 2020 entry (n=172, +0.076 R, t=2.3). It does not
    survive the capital cost: the 2.5 ATR stop sits a MEDIAN 22% below entry, so 1% of
    equity at risk buys a 4.5% position, and the portfolio simulation returns 0.6-2.4%
    CAGR against NIFTY's 10.9% over the same decade. Statistically non-zero, and not
    worth the slot. See scratchpad/run_phoenix*.py for the runs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class PhoenixParams:
    # ---- the drop -------------------------------------------------------
    roc_len: int = 14                 # "percentage difference ... 14 days earlier"
    roc_threshold: float = -15.0      # article's code value; -20 is the spoken rule
    confirm_lag: int = 3              # "ROC today more positive than 3 days earlier"

    # ---- exits ----------------------------------------------------------
    atr_len: int = 14
    atr_mode: str = "sma"             # sma = the source's rolling mean; rma = Wilder
    tp_atr: float = 1.0               # "Target is 1 times ATR"
    sl_atr: float = 2.5               # "Stop loss is 2.5 times ATR"

    # ---- hygiene --------------------------------------------------------
    min_price: float = 50.0           # a % rate-of-change on a penny print is noise


class Phoenix(SwingStrategy):
    name = "Phoenix Bird ROC rebound (1 ATR target / 2.5 ATR stop)"
    tag = "PHOENIX"

    def prepare(self, df: pd.DataFrame, p: PhoenixParams) -> pd.DataFrame:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        d["day"] = pd.Series(d.index, index=d.index).dt.date

        roc = d["Close"].pct_change(p.roc_len) * 100.0
        tr = ind.true_range(d)
        atr = tr.rolling(p.atr_len).mean() if p.atr_mode == "sma" else ind.rma(tr, p.atr_len)

        # every column the entry reads is as of YESTERDAY's close
        d["s_roc"] = roc.shift(1)
        d["s_roc_1"] = roc.shift(2)
        d["s_roc_n"] = roc.shift(1 + p.confirm_lag)
        d["s_atr"] = atr.shift(1)
        return d.dropna(subset=["s_roc", "s_roc_1", "s_roc_n", "s_atr"])

    def prepare_key(self, p: PhoenixParams) -> tuple:
        return (p.roc_len, p.confirm_lag, p.atr_len, p.atr_mode)

    def entry_at_open(self, c: Ctx, i: int, p: PhoenixParams, direction: int):
        if direction != 1:            # "This is a long-only strategy and hence no shorts"
            return None
        op, atr = c["Open"][i], c["s_atr"][i]
        roc, roc_1, roc_n = c["s_roc"][i], c["s_roc_1"][i], c["s_roc_n"][i]
        if not np.isfinite(op) or not np.isfinite(atr) or atr <= 0:
            return None
        if op < p.min_price:
            return None
        if not (roc < p.roc_threshold and roc > roc_1 and roc > roc_n):
            return None
        return EntrySignal(direction=1, sl=float(op - p.sl_atr * atr),
                           tp=float(op + p.tp_atr * atr), tag="ROC_REBOUND")
