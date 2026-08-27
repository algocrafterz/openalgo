"""Extreme-RSI breakaway gap - backtest adapter.

Source: "Trading Strategy with 1:10X Risk Reward", Kirubakaran Rajendran, 2026-07-22
(youtube uLTaXTyDxiI, 24.6k views, 1.13k likes, 85 comments). The video is in Tamil and
the auto-captions are poor, so every rule below is annotated with what it is derived
from - transcript, an author reply in the comments, or an assumption made here because
the video left the rule open.

THE IDEA
    Retail chases win rate; professionals chase payoff. A setup with 30% accuracy and
    1:10 reward is better than 70% at 1:1. The setup chosen to deliver that payoff is
    the BREAKAWAY GAP out of an exhausted trend:

      long   RSI in the extreme OVERSOLD zone, then the stock GAPS UP     -> buy the open
      short  RSI in the extreme OVERBOUGHT zone, then the stock GAPS DOWN -> sell the open

    The stop is the previous close - i.e. "the gap filled, the thesis is dead" - which
    is what makes the risk unit tiny and the advertised payoff large. There is no fixed
    target; the position is held as a swing until price closes back through the 21 EMA.

WHAT THE VIDEO DOES NOT SPECIFY (each is an input here, not a constant)
    - how big a gap counts as a gap          -> min_gap_pct, asked by @pushkardivedula162
    - RSI length: 14 in the video, 21 in the
      slide, 10 in another slide             -> rsi_len, asked by @MVignesh-i2o, @srisiva1265
    - which series the RSI is computed on    -> rsi_mode, see below
    - how the universe is filtered           -> not answered by the author for ~12 askers
    - whether an EMA cross is also required  -> require_ema_cross, asked by @abnirmal

THE RSI AMBIGUITY IS THE CENTRE OF THE STRATEGY
    The author gave two DIFFERENT answers in the comments:
      to @AnandanK_0729: "First add the EMA to your chart, then open RSI settings and
          check the Source dropdown, EMA will appear there."          -> RSI **of** the EMA
      to @sekarkanna1038:"The EMA option is under the Smoothing section ... that applies
          the EMA on the RSI line."                                   -> EMA **of** the RSI
    Only the first can produce the 1.67 reading shown on screen: RSI of a 21 EMA is
    computed on a near-monotone series, so in a downtrend nearly every delta is negative
    and it pins close to zero. A 14-EMA smoothing of a plain RSI floors far higher. Both
    are implemented (`rsi_mode`) plus the plain-price baseline, and the data ranks them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class GapRsiParams:
    # ---- indicators ----------------------------------------------------
    ema_len: int = 21                 # "21-day moving average" (transcript, explicit)
    rsi_len: int = 14                 # "default ... 14 period RSI" (transcript)
    rsi_mode: str = "on_ema"          # on_ema | smoothed | plain  (see module docstring)
    rsi_smooth_len: int = 14          # only used by rsi_mode="smoothed"

    # ---- setup ---------------------------------------------------------
    os_level: float = 10.0            # "10 10 the lower band upper band ... over 90"
    ob_level: float = 90.0            # -> extreme bands at 10 / 90, not 30 / 70
    min_gap_pct: float = 1.0          # ASSUMPTION: video never defines "a gap"
    max_gap_pct: float = 20.0         # ASSUMPTION: >20% is a corporate action or news shock

    # ---- risk and exit -------------------------------------------------
    stop_mode: str = "prev_close"     # prev_close | prev_extreme (transcript names both)
    tp_r: float = 0.0                 # 0 = open-ended, as in the video
    ema_exit_arm: bool = True         # see custom_exit(): the exit must arm before it fires
    ema_exit_delay: int = 0           # 1 = decide on yesterday's close, exit today

    # ---- filters ------------------------------------------------------
    min_price: float = 50.0           # penny prints make the gap% meaningless
    require_ema_cross: bool = False   # @abnirmal: must the open also clear the 21 EMA?


class GapRsi(SwingStrategy):
    name = "Extreme-RSI breakaway gap (1:10 R:R)"
    tag = "GAPRSI"
    pine = "signal_engine/pinescripts/swing/gap-rsi/gap-rsi-swing.pine"

    # ---- preparation ---------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: GapRsiParams) -> pd.DataFrame:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        d["day"] = pd.Series(d.index, index=d.index).dt.date

        e = ind.ema(d["Close"], p.ema_len)
        if p.rsi_mode == "on_ema":
            r = ind.rsi(e, p.rsi_len)
        elif p.rsi_mode == "smoothed":
            r = ind.ema(ind.rsi(d["Close"], p.rsi_len), p.rsi_smooth_len)
        elif p.rsi_mode == "plain":
            r = ind.rsi(d["Close"], p.rsi_len)
        else:
            raise ValueError(f"unknown rsi_mode {p.rsi_mode!r}")

        d["ema"] = e
        d["rsi"] = r
        # everything the entry reads must be as of YESTERDAY's close
        d["p_rsi"] = r.shift(1)
        d["p_close"] = d["Close"].shift(1)
        d["p_high"] = d["High"].shift(1)
        d["p_low"] = d["Low"].shift(1)
        d["p_ema"] = e.shift(1)
        d["gap_pct"] = (d["Open"] / d["p_close"] - 1.0) * 100.0
        # exit-side helpers
        d["above_ema"] = (d["Close"] > e).astype(float)
        d["below_ema"] = (d["Close"] < e).astype(float)
        return d.dropna(subset=["p_rsi", "p_close", "ema"])

    def prepare_key(self, p: GapRsiParams) -> tuple:
        return (p.ema_len, p.rsi_len, p.rsi_mode, p.rsi_smooth_len)

    # ---- entry ---------------------------------------------------------

    def entry_at_open(self, c: Ctx, i: int, p: GapRsiParams, direction: int):
        if i < 1:
            return None
        op = c["Open"][i]
        p_close, p_rsi = c["p_close"][i], c["p_rsi"][i]
        if not np.isfinite(op) or not np.isfinite(p_close) or not np.isfinite(p_rsi):
            return None
        if op < p.min_price:
            return None

        gap = c["gap_pct"][i]
        if direction == 1:
            # exhausted downtrend, then a gap UP: the breakaway
            if p_rsi >= p.os_level or gap < p.min_gap_pct or gap > p.max_gap_pct:
                return None
            sl = p_close if p.stop_mode == "prev_close" else min(p_close, c["p_low"][i])
            if p.require_ema_cross and not (op > c["p_ema"][i]):
                return None
        else:
            if p_rsi <= p.ob_level or -gap < p.min_gap_pct or -gap > p.max_gap_pct:
                return None
            sl = p_close if p.stop_mode == "prev_close" else max(p_close, c["p_high"][i])
            if p.require_ema_cross and not (op < c["p_ema"][i]):
                return None

        risk = abs(op - sl)
        if p.tp_r > 0:
            tp = op + direction * p.tp_r * risk
        else:
            tp = np.inf if direction == 1 else -np.inf
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp),
                           tag=f"{'OS_GAPUP' if direction == 1 else 'OB_GAPDN'}")

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._armed = False

    def reset_symbol(self, p) -> None:
        self._armed = False

    # ---- exit ----------------------------------------------------------

    def custom_exit(self, c: Ctx, i: int, p: GapRsiParams, pos: Position) -> str | None:
        """"until the stock closes below the 21 day moving average" (transcript).

        Taken literally this exits on the entry bar every time: a stock oversold enough
        to read RSI < 10 is trading well BELOW its 21 EMA, which is why it is oversold.
        The rule only makes sense as a trend exit, so it ARMS on the first close beyond
        the EMA and fires on the first close back through it. `ema_exit_arm=False`
        keeps the literal reading, for comparison.
        """
        j = i - p.ema_exit_delay
        if j < 0:
            return None
        beyond = c["above_ema"][j] if pos.direction == 1 else c["below_ema"][j]
        back = c["below_ema"][j] if pos.direction == 1 else c["above_ema"][j]
        if beyond:
            self._armed = True
            return None
        if back and (self._armed or not p.ema_exit_arm):
            return "EMA_EXIT"
        return None
