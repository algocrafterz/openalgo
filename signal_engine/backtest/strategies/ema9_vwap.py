"""The plain 9 EMA / VWAP crossover, tested as its own hypothesis.

WHY THIS IS SEPARATE FROM ema9_pdf.py
    `ema9_pdf.Ema9Pdf(variant="cvwap")` also crosses the 9 EMA against VWAP, but only
    AFTER a previous-day high/low break has already armed the setup. That is a
    compound hypothesis: a key-level break, plus a crossover trigger. If it fails,
    the break and the cross are indistinguishable as the cause.

    This adapter tests the crossover ALONE - no break precondition, no candlestick
    confirmation - which is the trader's stated observation, and then adds each
    proposed refinement as a single toggle so `ablation()` can price it separately:

        mode="anticipate"   fire as the gap closes, 1-2 bars BEFORE the cross
        require_key_level   only take the cross when it happens near a key level
        require_trend       200 EMA side filter
        exit_on_recross     leave when the 9 EMA crosses back over VWAP

    The claim under test is directional: "price respects the 9 EMA over VWAP".
    Anticipating the cross and siting it at a key level are claims about WHERE the
    good fills are, and they are only meaningful if the base cross clears zero first.

RISK MODEL IS SUPPLIED HERE, NOT BY THE OBSERVATION
    A crossover is an entry trigger. It carries no stop and no target, so both are
    chosen here and swept - which is itself the most important thing to know about
    the idea. `sl_mode` picks the geometry: a swing stop, an ATR stop, or a stop
    parked on the far side of VWAP (the version that matches the premise most
    closely - if price stops respecting VWAP, the reason for the trade is gone).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class Ema9VwapParams:
    #: cross      - 9 EMA crosses VWAP on a confirmed bar. The bare claim.
    #: anticipate - gap is closing and inside `anticipate_atr`, fire before the cross.
    mode: str = "cross"

    fast_len: int = 9
    atr_len: int = 14
    or_minutes: int = 15

    # ---- anticipation ----------------------------------------------------
    anticipate_atr: float = 0.15      # how close to VWAP counts as "approaching"

    # ---- optional filters, each priced separately by ablation() ----------
    require_key_level: bool = False
    key_level_atr: float = 0.35       # proximity to PDH/PDL/PDC/ORH/ORL
    require_trend: bool = False
    trend_len: int = 200

    # ---- risk, supplied here ---------------------------------------------
    sl_mode: str = "swing"            # swing | atr | vwap
    swing_lookback: int = 8
    sl_buffer_atr: float = 0.10
    sl_atr_mult: float = 1.00
    tp_r: float = 2.0

    exit_on_recross: bool = False

    #: Take the cross in the OPPOSITE direction. The follow-through study says
    #: the cross mean-reverts, so this prices the fade against the same costs.
    invert: bool = False

    min_price: float = 20.0

    def with_(self, **kw) -> Ema9VwapParams:
        return replace(self, **kw)


class Ema9Vwap(Strategy):
    name = "9 EMA / VWAP crossover, plain"
    tag = "EMA9VWAP"
    pine = "signal_engine/pinescripts/intraday/ema9-vwap/ema9-vwap.pine"

    # A crossover is not an opening-range setup - it fires whenever the two lines
    # meet. Cutting entries at 11:00 would measure a fraction of its own signals and
    # read as a weak strategy rather than a truncated test.
    run_overrides = {"entry_cutoff_min": 14 * 60, "max_trades_per_day": 3}

    def prepare_key(self, p) -> tuple:
        return (p.fast_len, p.atr_len, p.or_minutes, p.swing_lookback, p.trend_len)

    def prepare(self, df: pd.DataFrame, p: Ema9VwapParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        d["ema_f"] = ind.ema(d["Close"], p.fast_len)
        d["vwap"] = ind.session_vwap(d, day)
        d["atr"] = ind.atr(d, p.atr_len)
        d["gap"] = d["ema_f"] - d["vwap"]
        d["ema_t"] = ind.ema(d["Close"], p.trend_len)

        pdh, pdl = ind.prev_day_levels(d, day)
        d["pdh"], d["pdl"] = pdh, pdl
        d["pdc"] = day.map(d.groupby(day)["Close"].last().shift(1))
        orh, orl = ind.opening_range(d, day, p.or_minutes)
        d["orh"], d["orl"] = orh, orl

        d["swing_hi"] = d["High"].rolling(p.swing_lookback).max()
        d["swing_lo"] = d["Low"].rolling(p.swing_lookback).min()
        return d

    # ---- entry -----------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: Ema9VwapParams, direction: int) -> EntrySignal | None:
        if i < 1:
            return None
        px = float(c["Close"][i])
        atr = float(c["atr"][i])
        # NaN fails every naive comparison in BOTH directions, so guard explicitly
        # rather than relying on `atr <= 0` to catch the warmup window.
        if np.isnan(px) or np.isnan(atr) or atr <= 0 or px < p.min_price:
            return None

        g, g_prev = float(c["gap"][i]), float(c["gap"][i - 1])
        if np.isnan(g) or np.isnan(g_prev):
            return None

        if p.mode == "cross":
            fired = (g > 0 >= g_prev) if direction == 1 else (g < 0 <= g_prev)
        elif p.mode == "anticipate":
            # Not crossed yet, inside the band, and the gap is narrowing toward zero
            # with the bar closing on the side the cross would take it.
            near = abs(g) < p.anticipate_atr * atr
            if direction == 1:
                fired = near and g <= 0 and g > g_prev and px > float(c["ema_f"][i])
            else:
                fired = near and g >= 0 and g < g_prev and px < float(c["ema_f"][i])
        else:
            raise ValueError(f"unknown mode {p.mode!r}")
        if p.invert:
            fired = (g < 0 <= g_prev) if direction == 1 else (g > 0 >= g_prev)
        if not fired:
            return None

        if p.require_trend:
            t = float(c["ema_t"][i])
            if np.isnan(t) or ((px <= t) if direction == 1 else (px >= t)):
                return None

        if p.require_key_level:
            tol = p.key_level_atr * atr
            levels = [float(c[k][i]) for k in ("pdh", "pdl", "pdc", "orh", "orl")]
            if not any(not np.isnan(L) and abs(px - L) <= tol for L in levels):
                return None

        sl = self._stop(c, i, p, direction, px, atr)
        if sl is None or np.isnan(sl):
            return None
        risk = abs(px - sl)
        if risk <= 0 or np.isnan(risk):
            return None
        tp = px + direction * p.tp_r * risk
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp),
                           tag=f"{p.mode}{'_kl' if p.require_key_level else ''}")

    def _stop(self, c: Ctx, i: int, p: Ema9VwapParams,
              direction: int, px: float, atr: float) -> float | None:
        buf = p.sl_buffer_atr * atr
        if p.sl_mode == "atr":
            return px - direction * p.sl_atr_mult * atr
        if p.sl_mode == "vwap":
            # The premise is that VWAP is the line being respected. If price closes
            # through it the reason for the trade is gone, so the stop sits there -
            # widened to the swing when VWAP is nearer than the recent structure.
            v = float(c["vwap"][i])
            s = float(c["swing_lo"][i] if direction == 1 else c["swing_hi"][i])
            if np.isnan(v):
                return None
            lvl = min(v, s) if direction == 1 else max(v, s)
            if np.isnan(lvl):
                lvl = v
            return lvl - direction * buf
        s = float(c["swing_lo"][i] if direction == 1 else c["swing_hi"][i])
        if np.isnan(s):
            return None
        return s - direction * buf

    # ---- exit ------------------------------------------------------------

    def custom_exit(self, c: Ctx, i: int, p: Ema9VwapParams, pos: Position) -> str | None:
        if not p.exit_on_recross or i < 1:
            return None
        g, g_prev = float(c["gap"][i]), float(c["gap"][i - 1])
        if np.isnan(g) or np.isnan(g_prev):
            return None
        if pos.direction == 1 and g < 0 <= g_prev:
            return "RECROSS"
        if pos.direction == -1 and g > 0 >= g_prev:
            return "RECROSS"
        return None
