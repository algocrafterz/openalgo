"""EMA-20 pullback continuation - backtest adapter (candidate, not yet run).

Source: stockkhoj.in (free NSE/BSE screener), swing-scan category. Its rule, as stated
on the marketing page, is a one-liner - "pullback to 20 EMA during uptrend" - with no
entry trigger, stop, or target specified. Everything below the one-liner is this
codebase's own design, written so the idea is testable rather than argued about, same
approach as gap_rsi.py.

THE IDEA
    Standard trend-following continuation entry (IBD/Minervini "buy the first
    pullback"): a stock in a confirmed uptrend that has already shown strength pulls
    back toward its rising 20 EMA, then a green reversal bar shows buyers stepping back
    in before the medium-term trend (50 EMA) is broken. Buy the next open, sell if the
    trend actually breaks. This is a LONG-ONLY CNC/delivery swing, matching how RSI2
    mean-reversion (signal_engine/pinescripts/swing/rsi-tp-mr/) is already traded -
    India's T+1 settlement makes swing shorting in cash impractical.

WHAT STOCKKHOJ DOES NOT SPECIFY (each is an input here, not a constant)
    - how far price must have run above the 20 EMA to count as "extended" -> ext_pct
    - how close the pullback must come to the 20 EMA                     -> touch_band_pct
    - how the reversal is confirmed (a specific candle shape)            -> close_position_pct
    - the stop and target                                                -> stop_mode, tp_r
    - what "uptrend" means precisely                                     -> ema_mid/ema_slow alignment

WHY IT MIGHT COMPLEMENT THE EXISTING BOOK
    RSI2 mean-reversion (signal_engine/pinescripts/swing/rsi-tp-mr/) buys weakness
    against a longer-term trend and holds a few days. This buys STRENGTH after a
    shallow dip and is meant to be held while the trend runs - a different regime bet
    (trend continuation vs. mean reversion), not a variant of the same edge.

STATUS: Candidate. No .pine exists - this may never need one, since RSI2 already
proves a Python-only swing setup can run through signal_engine without a PineScript
front end. NOT backtested yet: blocked on a 10y Historify daily pull for the F&O
universe. See STRATEGY-ANALYSIS.md in
signal_engine/pinescripts/swing/ema-pullback/ for the full writeup.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class EmaPullbackParams:
    # ---- trend definition ----------------------------------------------
    ema_fast: int = 20                  # the EMA being pulled back to
    ema_mid: int = 50                   # medium-term trend / stop reference
    ema_slow: int = 200                 # long-term uptrend filter
    require_fast_above_mid: bool = False  # stricter: also require EMA20 > EMA50

    # ---- "was extended" filter ------------------------------------------
    ext_pct: float = 3.0                # close must have cleared EMA20 by this % ...
    lookback_bars: int = 10             # ... at least once in the N bars BEFORE the pullback

    # ---- pullback + reversal bar -----------------------------------------
    touch_band_pct: float = 1.0         # low must come within this % ABOVE EMA20
    pullback_floor_pct: float = 3.0     # low must not undercut EMA50 by more than this %
    close_position_pct: float = 50.0    # close must sit in the top X% of the day's range

    # ---- risk and exit ---------------------------------------------------
    stop_mode: str = "pullback_low"     # pullback_low | tighter_of_low_and_ema50
    sl_buffer_pct: float = 0.3          # extra room below the stop level
    tp_r: float = 0.0                   # 0 = open-ended (ride the trend exit)
    trend_exit_buffer_pct: float = 1.0  # close below EMA_mid * (1 - this%) exits
    trail_to_breakeven_r: float = 1.0   # 0/None disables; R multiple that arms breakeven

    # ---- filters -----------------------------------------------------
    min_price: float = 50.0             # penny prints make the % bands meaningless


class EmaPullback(SwingStrategy):
    name = "EMA-20 pullback continuation (stockkhoj.in swing scan)"
    tag = "EMAPB"
    pine = ""  # Python-only candidate; no PineScript port exists (or may ever be needed)

    # ---- preparation -----------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: EmaPullbackParams) -> pd.DataFrame:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        d["day"] = pd.Series(d.index, index=d.index).dt.date

        ema_f = ind.ema(d["Close"], p.ema_fast)
        ema_m = ind.ema(d["Close"], p.ema_mid)
        ema_s = ind.ema(d["Close"], p.ema_slow)

        uptrend = (d["Close"] > ema_s) & (ema_m > ema_s)
        if p.require_fast_above_mid:
            uptrend &= ema_f > ema_m

        extended = d["Close"] > ema_f * (1 + p.ext_pct / 100.0)
        # extension must predate the pullback bar itself: shift(1) drops today off the
        # window, then look back lookback_bars sessions before that.
        was_extended = (
            extended.shift(1).rolling(p.lookback_bars, min_periods=1).max().fillna(0).astype(bool)
        )

        touched = (d["Low"] <= ema_f * (1 + p.touch_band_pct / 100.0)) & (
            d["Low"] >= ema_m * (1 - p.pullback_floor_pct / 100.0)
        )
        day_range = (d["High"] - d["Low"]).replace(0, np.nan)
        close_in_upper_range = (d["Close"] - d["Low"]) >= (p.close_position_pct / 100.0) * day_range
        green_reversal = (d["Close"] > d["Open"]) & (d["Close"] > ema_f) & close_in_upper_range.fillna(False)

        pullback_bar = uptrend & was_extended & touched & green_reversal

        d["ema_f"] = ema_f
        d["ema_m"] = ema_m
        d["ema_s"] = ema_s
        # everything entry_at_open() reads must be as of the last CLOSED bar (i-1)
        d["p_signal"] = pullback_bar.shift(1, fill_value=False)
        d["p_low"] = d["Low"].shift(1)
        d["p_ema_m"] = ema_m.shift(1)
        return d.dropna(subset=["ema_f", "ema_m", "ema_s"])

    def prepare_key(self, p: EmaPullbackParams) -> tuple:
        return (
            p.ema_fast, p.ema_mid, p.ema_slow, p.require_fast_above_mid,
            p.ext_pct, p.lookback_bars, p.touch_band_pct, p.pullback_floor_pct,
            p.close_position_pct,
        )

    # ---- entry -------------------------------------------------------------

    def entry_at_open(self, c: Ctx, i: int, p: EmaPullbackParams, direction: int):
        if direction != 1:
            return None  # long-only: no cash-market swing shorting (T+1 settlement)
        if i < 1 or not c["p_signal"][i]:
            return None

        op = c["Open"][i]
        p_low, p_ema_m = c["p_low"][i], c["p_ema_m"][i]
        if not np.isfinite(op) or op < p.min_price:
            return None

        stop_level = p_low if p.stop_mode == "pullback_low" else min(p_low, p_ema_m)
        sl = stop_level * (1 - p.sl_buffer_pct / 100.0)
        risk = op - sl
        if risk <= 0:
            return None

        tp = op + p.tp_r * risk if p.tp_r > 0 else np.inf
        return EntrySignal(direction=1, sl=float(sl), tp=float(tp), tag="EMA_PULLBACK")

    def reset_symbol(self, p) -> None:
        self._be_armed = False

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._be_armed = False

    # ---- exit ---------------------------------------------------------------

    def trail(self, c: Ctx, i: int, p: EmaPullbackParams, pos: Position) -> float | None:
        """Ratchet the stop to breakeven once the trade has moved trail_to_breakeven_r."""
        if not p.trail_to_breakeven_r or self._be_armed:
            return None
        gain_r = (c["Close"][i] - pos.entry) / pos.risk
        if gain_r >= p.trail_to_breakeven_r:
            self._be_armed = True
            return pos.entry
        return None

    def custom_exit(self, c: Ctx, i: int, p: EmaPullbackParams, pos: Position) -> str | None:
        """Trend-break exit: close below EMA_mid means the continuation thesis failed."""
        if c["Close"][i] < c["ema_m"][i] * (1 - p.trend_exit_buffer_pct / 100.0):
            return "TREND_BREAK"
        return None
