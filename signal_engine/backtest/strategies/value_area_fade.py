"""Value-area rejection fade - backtest adapter.

A dedicated adapter for ONE of `open_drive.py` v2's three setups - VAL-REJ / VAH-REJ,
the "rejection" family from `volume-profile-model.md` - built with its own stop and
target rather than reusing v2's generic ones. Two things drove pulling this out into
its own file instead of just isolating it inside `open_drive.py` via toggles:

  1. It is a DIFFERENT BET. Acceptance and retest are continuation trades (price
     holds beyond the value area, bet it keeps going). Rejection is a MEAN-REVERSION
     trade (price failed to hold beyond the value area, bet it reverts toward the
     center of the auction). A continuation-style trend filter does not belong on a
     reversion trade - see `use_vwap_filter` below.
  2. v2's "nearest structural level" target was the single biggest driver of that
     version's -81.67 t-stat: on 1-minute bars some level (especially the
     still-forming Initial Balance) sits a few paise from price almost constantly,
     so touching "Target 1" became nearly free and nearly worthless. A rejection
     trade's natural target is specifically POC (`volume-profile-model.md`: "Trade
     the reversion toward POC / the opposite edge") - not the same seven-level grab
     bag - and even POC needs a floor (`tp_min_r`) so a POC that happens to sit close
     to the entry cannot reproduce the same bug in miniature.

THE TRADE
    VAL-REJ (long): a bar's Low wicks below the previous session's Value Area Low
    and its Close reclaims back above VAL - price probed a discount and was rejected.
    Bet on reversion up toward POC. VAH-REJ (short) mirrors this at the Value Area
    High, betting on reversion down toward POC.

SELECTIVITY
    The first cut of this adapter fired on ANY wick through the level, however
    shallow - 78,040 trades over 906 days against a ~1 gross-bp/trade edge (see
    STRATEGY-ANALYSIS.md's v3 section). `min_wick_depth_atr` requires the wick to
    have gone a minimum ATR-multiple PAST the level before counting as a rejection,
    on the theory that a deeper probe is closer to genuine failed price discovery
    than the noise of a level being grazed. Off by default (0.0 = any wick counts,
    reproducing v3 exactly) so its effect can be measured, not assumed.

STOP
    Beyond whichever is safer: the level itself, or the bar's own wick extreme
    (the wick already went further than the level - a stop at the level alone could
    sit INSIDE a wick that already happened), ATR-buffered.

TARGET
    POC, if it lies ahead of entry in the trade's direction, floored at `tp_min_r`
    risk-multiples (so a close POC cannot make the target trivially cheap to reach)
    and capped at `tp_r_cap` (so a distant POC cannot promise an implausible reward).
    Falls back to a flat risk-multiple (`tp_r_fallback`) when POC is unavailable or
    already behind price.

TIMING
    The model's own two entry windows (SS4E): 09:15-11:00 and 13:00-14:45 IST,
    excluding the 11:00-13:00 lunch lull. The engine's `RunConfig` only expresses one
    contiguous window, so the lunch exclusion is checked directly in `entry()`
    against the outer bound set via `run_overrides`. Unlike `open_drive.py`'s
    continuation setups, a reversion fade is not specifically an OPENING phenomenon
    - value-area edges get probed and rejected all day - so there is no reason to
    restrict this to the morning the way v1/v2 did.

PREVIOUS VALUE AREA
    From `signal_engine.backtest.volume_profile.prev_session_value_area()`. See that
    module's docstring for what it does and does not capture.

MULTI-SESSION SELECTIVITY (v7)
    Three additional OFF-by-default filters, added after v1-v6 (see
    STRATEGY-ANALYSIS.md) established that single-session wick-depth selectivity
    alone narrows the gap to real trading cost from ~16x to ~2.3-2.7x but does not
    close it, and exhausts its own sample at depth ~5.0. All three are computable
    from OHLCV alone - no orderflow/footprint data is available for backtesting here
    (Historify's `market_data` table stores only open/high/low/close/volume/oi; no
    Indian broker's historical API replays depth or footprint) - and each is a raw
    ratio computed in prepare(), thresholded in entry(), exactly like
    `min_wick_depth_atr`, so `prepare_key()` only grows for the two that change a
    COMPUTED VALUE (`ib_minutes`, `htf_ema_len`), not for the thresholds themselves.

    - `day_type_ib_ratio_max`: Market Profile's own guidance is that value-area fades
      are a Normal/balance-day tactic, not a Trend-day one. Gates on the PRIOR
      session's day-range/Initial-Balance-range ratio (`indicators.initial_balance_ratio`).
    - `va_confluence_atr_mult`: does the level being faded roughly agree with session
      N-2's value area (POC/VAL/VAH), not just N-1's? Multi-day agreement is a
      recognised Market Profile confluence signal ("naked POC" reasoning in reverse).
    - `htf_trend_atr_mult`: skip a fade betting against a strong multi-day trend
      (`indicators.prev_daily_trend_z`) - independent research into ICT-style
      liquidity/FVG signals found weak-to-null edges even on lower-cost, higher-
      liquidity markets than NSE intraday, so this stays a simple HTF-trend-strength
      gate rather than an ICT-specific entry trigger.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest import volume_profile as vp
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class FadeParams:
    value_area_pct: float = 70.0

    # ---- candle quality: CLV = (Close-Low)/(High-Low) ----------------------
    use_clv: bool = True
    clv_long_min: float = 0.65
    clv_short_max: float = 0.35

    # ---- participation -------------------------------------------------------
    use_volume: bool = True
    vol_mult: float = 1.5
    vol_ma_len: int = 20

    # ---- selectivity: how far the wick must go PAST the level, in ATR -------
    min_wick_depth_atr: float = 0.0

    # ---- trend filter - OFF by default. A rejection trade is a bet AGAINST the
    # move that just happened, which by construction usually sits on the "wrong"
    # side of VWAP at the moment of the wick. Kept as a toggle to verify that
    # intuition rather than assume it, not because it is expected to help.
    use_vwap_filter: bool = False

    min_price: float = 20.0

    # ---- timing: the model's two windows, lunch excluded --------------------
    morning_start_min: int = 9 * 60 + 15
    morning_end_min: int = 11 * 60
    afternoon_start_min: int = 13 * 60
    afternoon_end_min: int = 14 * 60 + 45

    # ---- stop: level-or-wick (whichever is safer), ATR-buffered -------------
    atr_len: int = 14
    stop_atr_mult: float = 0.3

    # ---- target: POC, floored and capped in risk-multiples -------------------
    tp_mode: str = "poc"          # poc | r
    tp_min_r: float = 1.0
    tp_r_cap: float = 4.0
    tp_r_fallback: float = 1.5    # tp_mode="r", or POC unavailable/behind price

    # ---- v7: multi-session selectivity, all OFF by default (0.0) -------------
    # day-type: require the PRIOR session's day-range/IB-range ratio at or below
    # this to count as a Normal/balance day. 0.0 = filter off.
    ib_minutes: int = 60
    day_type_ib_ratio_max: float = 0.0

    # multi-day confluence: require the level being faded to sit within this many
    # ATRs of session N-2's POC/VAL/VAH, not just N-1's. 0.0 = filter off.
    va_confluence_atr_mult: float = 0.0

    # HTF trend: skip a fade betting against a multi-day trend this strong, in units
    # of average daily range. 0.0 = filter off.
    htf_ema_len: int = 20
    htf_trend_atr_mult: float = 0.0


class ValueAreaFade(Strategy):
    name = "Value-Area Rejection Fade (VAL-REJ / VAH-REJ, mean reversion to POC)"
    tag = "VA_FADE"
    pine = ""   # Python-native; mirrors volume-profile-model.md, not a PineScript

    #: Outer bound only - the morning/afternoon split and lunch exclusion are
    #: enforced inside entry(), since RunConfig expresses one contiguous window.
    run_overrides = {"skip_open_minutes": 0, "entry_cutoff_min": 14 * 60 + 45}

    # ---- preparation ------------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: FadeParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        h, low, c, v = d["High"], d["Low"], d["Close"], d["Volume"]

        # Computed once, reused for BOTH the N-1 value area (existing) and the N-2
        # value area (v7 confluence) - value_area_at() is a cheap reindex/shift/map,
        # but session_value_area() itself runs MarketProfile per session and should
        # not be paid for twice.
        per_day_va = vp.session_value_area(d, day, p.value_area_pct)
        ppoc, pval, pvah = vp.value_area_at(per_day_va, day, 1)
        d["ppoc"], d["pval"], d["pvah"] = ppoc, pval, pvah
        d["ppoc2"], d["pval2"], d["pvah2"] = vp.value_area_at(per_day_va, day, 2)

        rng_safe = (h - low).replace(0, np.nan)
        d["clv"] = (c - low) / rng_safe

        d["vol_ratio"] = v / v.rolling(p.vol_ma_len, min_periods=5).mean().replace(0, np.nan)
        d["vwap"] = ind.session_vwap(d, day)
        atr = ind.atr(d, p.atr_len)
        d["atr"] = atr

        d["reject_up"] = (low < pval) & (c > pval)     # VAL-REJ: fade back up
        d["reject_dn"] = (h > pvah) & (c < pvah)        # VAH-REJ: fade back down
        # How far PAST the level the wick actually went, in ATR - a raw ratio, not
        # a boolean gate, so entry() can threshold it without prepare_key() ever
        # needing to know the threshold (see prepare_key()'s comment).
        atr_safe = atr.replace(0, np.nan)
        d["wick_depth_up"] = (pval - low) / atr_safe
        d["wick_depth_dn"] = (h - pvah) / atr_safe

        # v7: raw multi-session ratios, thresholded in entry() - same discipline as
        # wick_depth above, so these can be ablated without invalidating the cache.
        d["prior_ib_ratio"] = ind.initial_balance_ratio(d, day, p.ib_minutes)
        d["prior_htf_z"] = ind.prev_daily_trend_z(d, day, p.htf_ema_len, p.atr_len)
        return d

    def prepare_key(self, p: FadeParams) -> tuple:
        # Deliberately NOT the filter toggles or their thresholds - only fields
        # that change a COMPUTED VALUE belong here, so ablation() reuses one
        # prepare() pass per symbol instead of re-running the volume-profile
        # reconstruction for every variant. ib_minutes/htf_ema_len change a computed
        # VALUE (prior_ib_ratio/prior_htf_z), same reasoning as atr_len - the
        # thresholds that read them (day_type_ib_ratio_max/htf_trend_atr_mult) do not.
        return (p.value_area_pct, p.vol_ma_len, p.atr_len, p.ib_minutes, p.htf_ema_len)

    # ---- entry --------------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: FadeParams, direction: int):
        if i < 1:
            return None
        mins = c["mins"][i]
        in_morning = p.morning_start_min <= mins < p.morning_end_min
        in_afternoon = p.afternoon_start_min <= mins < p.afternoon_end_min
        if not (in_morning or in_afternoon):
            return None

        px = c["Close"][i]
        if not np.isfinite(px) or px < p.min_price:
            return None

        fired = c["reject_up"][i] if direction == 1 else c["reject_dn"][i]
        if not fired:
            return None

        if p.min_wick_depth_atr > 0.0:
            depth = c["wick_depth_up"][i] if direction == 1 else c["wick_depth_dn"][i]
            if not (np.isfinite(depth) and depth >= p.min_wick_depth_atr):
                return None

        if p.day_type_ib_ratio_max > 0.0:
            ratio = c["prior_ib_ratio"][i]
            if not (np.isfinite(ratio) and ratio <= p.day_type_ib_ratio_max):
                return None

        if p.use_clv:
            clv = c["clv"][i]
            if not np.isfinite(clv):
                return None
            if direction == 1 and clv < p.clv_long_min:
                return None
            if direction == -1 and clv > p.clv_short_max:
                return None

        if p.use_volume and not (c["vol_ratio"][i] >= p.vol_mult):
            return None

        if p.use_vwap_filter:
            vwap = c["vwap"][i]
            if not np.isfinite(vwap):
                return None
            if direction == 1 and px <= vwap:
                return None
            if direction == -1 and px >= vwap:
                return None

        if p.htf_trend_atr_mult > 0.0:
            z = c["prior_htf_z"][i]
            if not np.isfinite(z):
                return None
            # Don't fade UP into an established DOWNTREND, or fade DOWN into an
            # established UPTREND - a rejection is a weaker bet against a multi-day
            # trend this strong, not that it never works (see module docstring).
            if direction == 1 and z <= -p.htf_trend_atr_mult:
                return None
            if direction == -1 and z >= p.htf_trend_atr_mult:
                return None

        level = c["pval"][i] if direction == 1 else c["pvah"][i]
        if not np.isfinite(level):
            return None

        if p.va_confluence_atr_mult > 0.0:
            atr_now = c["atr"][i]
            if not (np.isfinite(atr_now) and atr_now > 0):
                return None
            candidates = (c["ppoc2"][i], c["pval2"][i], c["pvah2"][i])
            near = any(np.isfinite(x) and abs(level - x) <= p.va_confluence_atr_mult * atr_now
                       for x in candidates)
            if not near:
                return None

        atr = c["atr"][i]
        if not np.isfinite(atr) or atr <= 0:
            return None
        buf = atr * p.stop_atr_mult

        # The wick already went further than the level itself - the stop must
        # clear the ACTUAL EXTREME reached, not just the level.
        extreme = c["Low"][i] if direction == 1 else c["High"][i]
        edge = min(level, extreme) if direction == 1 else max(level, extreme)
        sl = edge - buf if direction == 1 else edge + buf
        risk = abs(px - sl)
        if risk <= 0 or not np.isfinite(risk):
            return None

        tp = self._target(c, i, p, px, risk, direction)
        tag = "VAL_REJ" if direction == 1 else "VAH_REJ"
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp), tag=tag)

    def _target(self, c: Ctx, i: int, p: FadeParams, px: float, risk: float,
                direction: int) -> float:
        if p.tp_mode == "r":
            return px + direction * risk * p.tp_r_fallback
        if p.tp_mode != "poc":
            raise ValueError(f"unknown tp_mode {p.tp_mode!r}")

        poc = c["ppoc"][i]
        ahead = np.isfinite(poc) and ((poc > px) if direction == 1 else (poc < px))
        if not ahead:
            return px + direction * risk * p.tp_r_fallback

        dist = abs(poc - px)
        dist = max(dist, risk * p.tp_min_r)     # floor: never trivially close
        dist = min(dist, risk * p.tp_r_cap)     # cap: never implausibly far
        return px + direction * dist
