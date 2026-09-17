"""IBS (Internal Bar Strength) mean reversion - backtest adapter.

Source: Pagonidis (2013), "The Internal Bar Strength Indicator", extended across
asset classes by Alvarez Quant Trading / QuantifiedStrategies with the SAME
parameters unchanged - that cross-market robustness (not overfit to one series) is
why this was picked as a swing candidate. Published evidence is mostly US
ETFs/indices; single-stock NSE F&O evidence is thin, which is exactly what this
backtest is for.

THE IDEA
    IBS = (Close - Low) / (High - Low), a 0-1 score of where a bar closed inside its
    own range. A close near the low (IBS near 0) statistically precedes a bounce the
    next session - short-term overreaction / liquidity-provision, not a trend call.
    Buy weak closes, sell strong closes.

WHY THIS DIFFERS FROM WHAT ALREADY FAILED HERE
    gap_rsi.py tested an RSI-extreme-plus-gap setup: no edge, median trade stopped
    out on the entry bar itself. IBS is range-position, not momentum-of-momentum -
    a different statistic, and it needs no gap to fire.

LONG-ONLY: CNC delivery cannot short, so only the "buy weak closes" half of the
    (symmetric, in the literature) edge is tradeable here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class IbsMeanRevParams:
    # ---- entry -----------------------------------------------------------
    ibs_buy_max: float = 0.20         # buy when YESTERDAY's IBS closed this low or lower
    min_price: float = 50.0           # penny prints make the range noisy

    # ---- optional falling-knife filter (off by default, ablation axis) ---
    #: A weak close in a genuine downtrend is not "overreaction", it is the trend.
    #: require_above_sma=True only buys a weak close that is still above its own
    #: longer-term average - a pullback inside an uptrend, not a bet the trend flips.
    use_trend_filter: bool = False
    trend_sma_len: int = 100
    require_above_sma: bool = True

    # ---- exit --------------------------------------------------------------
    ibs_sell_min: float = 0.80        # exit once TODAY's IBS closes this high or higher
    max_hold_days: int = 5            # time-stop if IBS never closes strong

    # ---- protective stop (a risk cap, not the exit signal itself) ----------
    atr_len: int = 14
    sl_atr_mult: float = 2.0


class IbsMeanRev(SwingStrategy):
    name = "IBS (Internal Bar Strength) mean reversion"
    tag = "IBSMR"
    pine = ""   # Python-only candidate, no PineScript source

    # ---- preparation -----------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: IbsMeanRevParams) -> pd.DataFrame:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        d["day"] = pd.Series(d.index, index=d.index).dt.date

        rng = (d["High"] - d["Low"]).replace(0, np.nan)
        ibs = (d["Close"] - d["Low"]) / rng
        d["ibs"] = ibs.fillna(0.5)
        d["p_ibs"] = d["ibs"].shift(1)
        d["p_close"] = d["Close"].shift(1)

        a = ind.atr(d, p.atr_len)
        d["atr"] = a
        d["p_atr"] = a.shift(1)

        sma = d["Close"].rolling(p.trend_sma_len).mean()
        above = (d["Close"] > sma).astype(float)
        d["p_above_sma"] = above.shift(1)

        return d.dropna(subset=["p_ibs", "p_close", "p_atr"])

    def prepare_key(self, p: IbsMeanRevParams) -> tuple:
        return (p.atr_len, p.trend_sma_len)

    # ---- entry -------------------------------------------------------------

    def entry_at_open(self, c: Ctx, i: int, p: IbsMeanRevParams, direction: int):
        if direction != 1 or i < 1:
            return None   # long-only: CNC delivery cannot short
        op = c["Open"][i]
        p_ibs, p_close, p_atr = c["p_ibs"][i], c["p_close"][i], c["p_atr"][i]
        if not (np.isfinite(op) and np.isfinite(p_ibs) and np.isfinite(p_close)
                and np.isfinite(p_atr) and p_atr > 0):
            return None
        if op < p.min_price:
            return None
        if p_ibs > p.ibs_buy_max:
            return None

        if p.use_trend_filter:
            above = c["p_above_sma"][i]
            has_sma = np.isfinite(above)
            if p.require_above_sma and not (has_sma and above > 0):
                return None
            if not p.require_above_sma and has_sma and above > 0:
                return None

        sl = op - p.sl_atr_mult * p_atr
        if sl <= 0 or sl >= op:
            return None
        # open-ended: custom_exit() below is the real exit, this SL is a crash floor
        return EntrySignal(direction=1, sl=float(sl), tp=float(np.inf), tag="IBS_LOW")

    # ---- exit ----------------------------------------------------------------

    def custom_exit(self, c: Ctx, i: int, p: IbsMeanRevParams, pos: Position) -> str | None:
        ibs = c["ibs"][i]
        if np.isfinite(ibs) and ibs >= p.ibs_sell_min:
            return "IBS_HIGH"
        if i - pos.entry_bar >= p.max_hold_days:
            return "TIME_EXIT"
        return None
