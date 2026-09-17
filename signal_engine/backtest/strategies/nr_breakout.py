"""NR7/NR4 narrow-range breakout - backtest adapter.

Source: Toby Crabel, "Day Trading with Short-Term Price Patterns" - trade a
breakout of the prior session's high/low FOR CONTINUATION, but only after that
prior session's own range was the narrowest of its trailing N sessions (N=7 is the
classic "NR7"; N=4, "NR4", is more selective/rarer). The idea: a volatility
squeeze precedes an expansion, so a breakout out of a coiled session is more likely
to run than an ordinary day's breakout.

WHY THIS ONE, SPECIFICALLY
    This repo's own key_level.py study already measured that breaking PDH/PDL
    follows through only 30.9% of the time vs a 33.3% random-walk baseline - i.e.
    breaking a level for CONTINUATION has no edge, UNCONDITIONALLY. Every
    continuation strategy tried here (orb, ema9, key_level, ib_extension) traded
    that break regardless of the volatility regime the prior day was in. This
    adapter reuses the EXACT SAME PDH/PDL break-for-continuation mechanism, gated
    on ONE new condition: was the level-setting day itself a narrow-range day. With
    `require_nr=False` this collapses to key_level.py's already-measured-negative
    PDH/PDL-break-only subset, which makes it a clean, direct A/B test of whether
    the volatility-regime filter changes anything, not a fresh unrelated idea.

    Also directly answers the earlier research finding on this: NR7 was flagged as
    "best used as a pre-filter layered on top of an existing signal, not a
    standalone system" (QuantifiedStrategies' own base-NR7 backtest called it "not
    worth trading" alone). Layering it onto a signal ALREADY PROVEN to have no
    edge (PDH/PDL breaks) is the honest first test - if NR-gating cannot even
    rescue a signal already measured near a random-walk baseline, it is unlikely
    to add value layered onto anything else either.

DEFINITION (Crabel, matching the existing `orb.pine` `enableNRFilter` input's own
    wording): a session's range (High-Low) is NR-qualifying when it is the
    narrowest of the trailing `nr_lookback` sessions INCLUDING itself. Evaluated
    once the session is complete, then shifted forward exactly the way
    `indicators.prev_day_levels` shifts PDH/PDL, so nothing here can see tomorrow's
    range before it happens.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class NrBreakoutParams:
    # ---- narrow-range definition -------------------------------------------
    nr_lookback: int = 7              # 7 = NR7 (classic), 4 = NR4 (rarer, stricter)
    #: Ablation control: False trades every PDH/PDL break regardless of the prior
    #: day's range - reproduces key_level.py's already-measured-negative subset.
    require_nr: bool = True

    # ---- setup ---------------------------------------------------------------
    atr_len: int = 14
    min_entry_min: int = 9 * 60 + 30
    min_price: float = 20.0
    cooldown_bars: int = 6

    # ---- risk ------------------------------------------------------------
    sl_buffer_atr: float = 0.25
    min_sl_atr: float = 0.4
    min_sl_pct_price: float = 0.003

    # ---- target ------------------------------------------------------------
    tp_r: float = 1.5


class NrBreakout(Strategy):
    name = "NR7/NR4 narrow-range breakout (Crabel), PDH/PDL for continuation"
    tag = "NRBREAKOUT"
    pine = ""   # Python-only candidate; orb.pine's enableNRFilter is a separate,
               # never-backtested live toggle this adapter exists to actually test

    # ---- preparation -----------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: NrBreakoutParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        h, low_ = d["High"], d["Low"]

        d["pdh"], d["pdl"] = ind.prev_day_levels(d, day)

        daily_high = h.groupby(day).max()
        daily_low = low_.groupby(day).min()
        daily_range = daily_high - daily_low
        is_nr_day = daily_range <= daily_range.rolling(
            p.nr_lookback, min_periods=p.nr_lookback).min()
        # was YESTERDAY (the session whose high/low we are trading) itself
        # NR-qualifying - same shift pattern prev_day_levels uses internally.
        d["is_nr_prev"] = day.map(is_nr_day.shift(1)).astype(float)

        d["atr"] = ind.atr(d, p.atr_len)
        return d

    def prepare_key(self, p: NrBreakoutParams) -> tuple:
        return (p.nr_lookback, p.atr_len)

    # ---- state -----------------------------------------------------------

    def reset_symbol(self, p) -> None:
        self._last_fire = -10_000

    def reset_session(self, p) -> None:
        self._last_fire = -10_000

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._last_fire = i

    # ---- entry -----------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: NrBreakoutParams, direction: int):
        if i < 2:
            return None
        px, prev = c["Close"][i], c["Close"][i - 1]
        if not np.isfinite(px) or px < p.min_price or c["mins"][i] < p.min_entry_min:
            return None
        if i - self._last_fire <= p.cooldown_bars:
            return None
        atr = c["atr"][i]
        if not np.isfinite(atr) or atr <= 0:
            return None
        if p.require_nr and not (c["is_nr_prev"][i] > 0):
            return None

        if direction == 1:
            lvl = c["pdh"][i]
            if not (np.isfinite(lvl) and px > lvl and prev <= lvl):
                return None
        else:
            lvl = c["pdl"][i]
            if not (np.isfinite(lvl) and px < lvl and prev >= lvl):
                return None

        if direction == 1:
            raw = c["Low"][i] - atr * p.sl_buffer_atr
        else:
            raw = c["High"][i] + atr * p.sl_buffer_atr
        min_dist = max(atr * p.min_sl_atr, px * p.min_sl_pct_price)
        sl = min(raw, px - min_dist) if direction == 1 else max(raw, px + min_dist)
        risk = abs(px - sl)
        if risk <= 0 or not np.isfinite(risk):
            return None

        tp = px + direction * risk * p.tp_r
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp), tag="NR_BRK")
