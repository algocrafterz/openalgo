"""Turtle Soup failed-breakout fade - backtest adapter.

Source: Linda Raschke / Laurence Connors, "Street Smarts" (1995) - the mirror image
of the original Turtle breakout system: instead of trading a breakout for
continuation, bet that it FAILS.

WHY THIS ONE, SPECIFICALLY
    This repo's own key_level.py study already measured that a break of PDH/PDL/
    IBH/IBL follows through only 30.9% of the time against a 33.3% random-walk
    baseline (t=-9.87, 49,677 events) - breaking a level is slightly WORSE than
    random, not better. Every strategy tried so far (orb, ema9, key_level,
    ib_extension) traded the break for continuation. This adapter fades the EXACT
    SAME levels key_level.py already built (PDH/PDL, optionally IBH/IBL) instead of
    inventing new ones, so the result is directly comparable to that study rather
    than a fresh, unrelated idea.

THE SETUP
    A "failed break" prints when a bar trades through a level intrabar but closes
    back on the origin side of it within the SAME bar:
        short  High[i] > level  AND  Close[i] < level   (failed break UP)
        long   Low[i]  < level  AND  Close[i] > level   (failed break DOWN)
    Stop sits just beyond the bar's own extreme (the false break itself, which is
    the level of maximum stop-hunting per Raschke's thesis). Target is a fixed
    R-multiple - no claim is made about a "measured move" back to the level's
    opposite side, since PDH-to-PDL can be a very large distance.

EVIDENCE HONESTY: folklore-grade, not a rigorous published backtest. Raschke's book
    documents it as a recurring pattern, not with formal significance statistics.
    That is exactly why this backtest exists - to find out if it clears this repo's
    own t-stat hurdle rather than take the pattern-book claim on faith.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class TurtleSoupParams:
    # ---- which levels to fade ---------------------------------------------
    use_pdh_pdl: bool = True          # fade a failed break of yesterday's high/low
    use_ib: bool = False              # also fade a failed break of the initial balance
    ib_minutes: int = 60              # 09:15-10:15, matches key_level.py's default

    # ---- setup ---------------------------------------------------------------
    atr_len: int = 14
    min_entry_min: int = 9 * 60 + 30  # skip the first 15 minutes of pure noise
    min_price: float = 20.0
    cooldown_bars: int = 6            # bars before the same symbol can fire again

    # ---- trend-day filter, off by default (ablation axis) --------------------
    #: A failed break IS the setup, but a genuine trend day breaks and holds - fading
    #: every false break regardless of context risks fading the one that runs.
    use_adx_filter: bool = False
    max_adx: float = 25.0             # skip fades when ADX reads a strong trend
    adx_len: int = 14

    # ---- risk ------------------------------------------------------------
    sl_buffer_atr: float = 0.25       # stop beyond the false-break extreme
    min_sl_atr: float = 0.4
    min_sl_pct_price: float = 0.003

    # ---- target ------------------------------------------------------------
    tp_r: float = 1.5


class TurtleSoup(Strategy):
    name = "Turtle Soup failed-breakout fade (PDH/PDL, optionally IBH/IBL)"
    tag = "TURTLESOUP"
    pine = ""   # Python-only candidate, no PineScript source

    # ---- preparation ---------------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: TurtleSoupParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        h, low_ = d["High"], d["Low"]

        d["pdh"], d["pdl"] = ind.prev_day_levels(d, day)

        in_ib = d["from_open"] < p.ib_minutes
        d["ibh"] = day.map(h.where(in_ib).groupby(day).max())
        d["ibl"] = day.map(low_.where(in_ib).groupby(day).min())
        d["ib_done"] = (~in_ib).astype(float)

        d["atr"] = ind.atr(d, p.atr_len)
        d["adx"] = ind.adx(d, p.adx_len) if p.use_adx_filter else np.nan
        return d

    def prepare_key(self, p: TurtleSoupParams) -> tuple:
        return (p.ib_minutes, p.atr_len, p.use_adx_filter, p.adx_len)

    # ---- state -----------------------------------------------------------

    def reset_symbol(self, p) -> None:
        self._last_fire = -10_000

    def reset_session(self, p) -> None:
        self._last_fire = -10_000

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._last_fire = i

    # ---- setup resolution --------------------------------------------------

    @staticmethod
    def _fails_high(c: Ctx, i: int, lvl: float) -> bool:
        return np.isfinite(lvl) and c["High"][i] > lvl and c["Close"][i] < lvl

    @staticmethod
    def _fails_low(c: Ctx, i: int, lvl: float) -> bool:
        return np.isfinite(lvl) and c["Low"][i] < lvl and c["Close"][i] > lvl

    def _resolve(self, c: Ctx, i: int, p: TurtleSoupParams, direction: int) -> tuple[str, bool]:
        """A failed break UP fades short; a failed break DOWN fades long."""
        if direction == -1:
            if p.use_pdh_pdl and self._fails_high(c, i, c["pdh"][i]):
                return "PDH_FADE", True
            if p.use_ib and c["ib_done"][i] > 0 and self._fails_high(c, i, c["ibh"][i]):
                return "IBH_FADE", True
            return "", False
        if p.use_pdh_pdl and self._fails_low(c, i, c["pdl"][i]):
            return "PDL_FADE", True
        if p.use_ib and c["ib_done"][i] > 0 and self._fails_low(c, i, c["ibl"][i]):
            return "IBL_FADE", True
        return "", False

    # ---- entry -----------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: TurtleSoupParams, direction: int):
        if i < 2:
            return None
        px = c["Close"][i]
        if not np.isfinite(px) or px < p.min_price or c["mins"][i] < p.min_entry_min:
            return None
        if i - self._last_fire <= p.cooldown_bars:
            return None
        atr = c["atr"][i]
        if not np.isfinite(atr) or atr <= 0:
            return None
        if p.use_adx_filter:
            adx_v = c["adx"][i]
            if np.isfinite(adx_v) and adx_v > p.max_adx:
                return None

        tag, ok = self._resolve(c, i, p, direction)
        if not ok:
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
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp), tag=tag)
