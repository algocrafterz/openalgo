"""Value-area edge scalp - backtest adapter, v2.

Rewritten against the repo's own documented model
(`signal_engine/pinescripts/intraday/volume-profile/volume-profile-model.md`, the
"Volume Profile Entry Zone" diagram) instead of an invented trigger. v1 required the
OPENING candle itself to be the qualifying shape and the trigger ("break of that
candle's own high") is not one of the model's six setups. This version implements
three of them directly - the three the model's own Day-Type section (SS8) says apply
when the SESSION opens beyond the value area, which is the case the user asked about:

    VAH-ACC / VAL-ACC   N consecutive closes beyond the level (SS4A "acceptance")
    VAH-RT  / VAL-RT    close breaks the level, price returns to a band around it,
                        and HOLDS beyond it again (SS4A "retest") - the model's
                        highest-priority setup
    VAH-REJ / VAL-REJ   a bar wicks through the level and closes back inside it
                        (SS4A "rejection") - the opposite trade: fading a failed
                        break, not required to open beyond the level at all

Priority when more than one fires on the same bar: retest > rejection > acceptance,
exactly as the model specifies (SS6).

WHY THE OPENING-CANDLE FRAMING SURVIVES ANYWAY
    The model's own SS8 "Day-Type Context" says a session that OPENS above VAH or
    below VAL "favours a trend day - lean on acceptance/continuation setups". That is
    the user's original premise, kept here as `use_day_type_filter` - a session-level
    CONTEXT flag, not a per-trade gate baked into every setup's location test. v1
    conflated the two: it required the OPEN itself to be beyond the level AND separate
    confirmation bars, which is stricter than the model's actual VAH-ACC definition
    ("two consecutive closes above VAH" - nothing about where the session opened).
    v1's own ablation had already found removing the open-based gate helped in both
    windows; this design explains why, rather than just averaging it away.

STOP AND TARGET, PER THE MODEL (SS4/SS5), NOT AN OPENING-CANDLE WIDTH
    Stop sits just beyond the LEVEL IN PLAY, ATR-buffered - not beyond the whole
    opening candle (v1's `candle_atr` mode, which is structurally guaranteed wide by
    the relative-range filter that no longer exists here). Target 1 is "the nearest
    structural level" - POC / VAH / VAL / PDH / PDL / IBH / IBL, whichever sits
    nearest ahead of the entry in the trade's direction - with a risk-multiple
    fallback when nothing structural is ahead (a continuation day that has already
    cleared every known level).

PREVIOUS VALUE AREA
    From `signal_engine.backtest.volume_profile.prev_session_value_area()` - a real
    reconstruction from 1-minute bars via the `MarketProfile` library. See that
    module's docstring for what it does and does not capture.

WHAT IS DELIBERATELY LEFT OUT
    The model's footprint/orderflow confirmation (SS7) is explicitly a discretionary,
    manual gate - "no footprint confirmation -> no trade, regardless of score" - and
    has no backtestable proxy in bar data. Its scoring system (SS6) is likewise built
    for a discretionary trader reading a live chart, not a parameter sweep; the
    filters here (CLV, RVOL, VWAP alignment, structural confluence) are the same
    ingredients without a single blended score, so each one can be ablated on its own
    rather than hidden inside a threshold. Sector/index confirmation is still not
    modelled, same reasoning as v1.
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
class OpenDriveParams:
    # ---- previous value area --------------------------------------------
    value_area_pct: float = 70.0
    #: SS8 day-type context: session must OPEN beyond the level for that direction's
    #: setups to be considered at all. Off by default - v1's ablation found the
    #: open-based gate hurt when it was the ONLY location test; here it is optional
    #: context on top of the model's real triggers, worth re-testing on its own.
    use_day_type_filter: bool = False

    # ---- setups (priority retest > rejection > acceptance, per the model) ------
    use_retest: bool = True
    use_rejection: bool = True
    use_acceptance: bool = True
    #: VAH-ACC / VAL-ACC: this many consecutive closes beyond the level.
    acceptance_bars: int = 2
    #: VAH-RT / VAL-RT: retest must occur within this many bars of the breakout.
    retest_max_bars: int = 8
    #: How close (in ATR) price must return to the level to count as "the retest".
    retest_band_atr: float = 0.5

    # ---- candle quality: CLV = (Close-Low)/(High-Low), per the model's SS4B ----
    use_clv: bool = True
    clv_long_min: float = 0.65
    clv_short_max: float = 0.35

    # ---- participation: RVOL vs a plain volume SMA, per the model's SS4C -------
    use_volume: bool = True
    vol_mult: float = 1.5
    vol_ma_len: int = 20

    # ---- trend filter, NOT a trigger - per the model's non-negotiable rule #2 --
    use_vwap_filter: bool = True

    min_price: float = 20.0

    # ---- stop: level in play, ATR-buffered (model SS4/SS5) ----------------------
    atr_len: int = 14
    stop_atr_mult: float = 0.3

    # ---- target: nearest structural level, model SS4/SS5 "Target 1" ------------
    tp_mode: str = "structural"       # structural | r
    #: Fallback target (as a multiple of risk) when tp_mode="r", or when no
    #: structural level lies ahead of entry in the trade's direction.
    tp_r_fallback: float = 1.5
    #: Sanity cap on a structural target that is implausibly far away (a session
    #: that has already cleared PDH/IBH by the time this bar prints) - expressed as
    #: a multiple of risk, not a price distance.
    tp_r_cap: float = 4.0


class OpenDrive(Strategy):
    name = "Value-Area Edge Scalp (retest / rejection / acceptance)"
    tag = "OPEN_DRIVE"
    pine = ""   # Python-native; mirrors volume-profile-model.md, not a PineScript

    #: The model's own morning window (SS4E). Entries only, positions may run past it.
    run_overrides = {"skip_open_minutes": 0, "entry_cutoff_min": 11 * 60}

    # ---- preparation ------------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: OpenDriveParams) -> pd.DataFrame:
        # Only RAW features here, never a filter's pass/fail - see prepare_key()'s
        # comment for why (the mistake that made v1 take hours to ablate instead of
        # minutes was baking toggles into prepare()-time columns).
        d = ind.add_session_columns(df)
        day = d["day"]
        o, h, low, c, v = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]

        ppoc, pval, pvah = vp.prev_session_value_area(d, day, p.value_area_pct)
        d["ppoc"], d["pval"], d["pvah"] = ppoc, pval, pvah

        pdh, pdl = ind.prev_day_levels(d, day)
        ibh, ibl = ind.opening_range(d, day, 60)   # Initial Balance, 09:15-10:15
        d["pdh"], d["pdl"], d["ibh"], d["ibl"] = pdh, pdl, ibh, ibl

        rng_safe = (h - low).replace(0, np.nan)
        d["clv"] = (c - low) / rng_safe

        d["vol_ratio"] = v / v.rolling(p.vol_ma_len, min_periods=5).mean().replace(0, np.nan)
        d["vwap"] = ind.session_vwap(d, day)
        d["atr"] = ind.atr(d, p.atr_len)

        # ---- acceptance: N consecutive closes beyond the level ---------------
        above_vah = (c > pvah).astype(int)
        below_val = (c < pval).astype(int)
        d["accept_up"] = above_vah.rolling(p.acceptance_bars).sum() == p.acceptance_bars
        d["accept_dn"] = below_val.rolling(p.acceptance_bars).sum() == p.acceptance_bars

        # ---- retest: broke the level, came back near it, holds beyond again ---
        broke_up = (c > pvah) & (c.shift(1) <= pvah)
        broke_dn = (c < pval) & (c.shift(1) >= pval)
        since_up = ind.bars_since(broke_up)
        since_dn = ind.bars_since(broke_dn)
        band = d["atr"] * p.retest_band_atr
        d["retest_up"] = ((since_up > 0) & (since_up <= p.retest_max_bars)
                          & (c > pvah) & ((c - pvah).abs() <= band))
        d["retest_dn"] = ((since_dn > 0) & (since_dn <= p.retest_max_bars)
                          & (c < pval) & ((pval - c).abs() <= band))

        # ---- rejection: wick through the level, close back inside -------------
        d["reject_up"] = (low < pval) & (c > pval)     # VAL-REJ (long: fade back up)
        d["reject_dn"] = (h > pvah) & (c < pvah)        # VAH-REJ (short: fade back down)

        # ---- day-type context: did the SESSION open beyond the level? ---------
        is_first = (d["from_open"] == 0)
        first_open = day.map(o.where(is_first).groupby(day).first())
        d["opened_above_vah"] = (first_open > pvah)
        d["opened_below_val"] = (first_open < pval)
        return d

    def prepare_key(self, p: OpenDriveParams) -> tuple:
        # Deliberately NOT the setup/filter toggles or their thresholds - only
        # fields that change a COMPUTED VALUE belong here, so the harness's
        # ablation() reuses one prepare() pass per symbol across every variant
        # instead of re-running the volume-profile reconstruction each time.
        return (p.value_area_pct, p.acceptance_bars, p.retest_max_bars,
                p.retest_band_atr, p.vol_ma_len, p.atr_len)

    # ---- entry --------------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: OpenDriveParams, direction: int):
        if i < 1:
            return None
        px = c["Close"][i]
        if not np.isfinite(px) or px < p.min_price:
            return None

        level_name, entry_reason = self._setup(c, i, p, direction)
        if level_name is None:
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

        if p.use_day_type_filter:
            opened_ok = c["opened_above_vah"][i] if direction == 1 else c["opened_below_val"][i]
            if not opened_ok:
                return None

        level = c[level_name][i]
        if not np.isfinite(level):
            return None

        atr = c["atr"][i]
        if not np.isfinite(atr) or atr <= 0:
            return None
        buf = atr * p.stop_atr_mult

        if entry_reason == "reject":
            # The level was already wicked through this bar - the stop must clear
            # the wick itself, not just the level, or the setup's own trigger bar
            # would stop the trade out.
            extreme = c["Low"][i] if direction == 1 else c["High"][i]
            edge = min(level, extreme) if direction == 1 else max(level, extreme)
        else:
            edge = level
        sl = edge - buf if direction == 1 else edge + buf
        risk = abs(px - sl)
        if risk <= 0 or not np.isfinite(risk):
            return None

        tp = self._target(c, i, p, px, risk, direction)
        tag = f"{entry_reason.upper()}_{'UP' if direction == 1 else 'DN'}"
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp), tag=tag)

    def _setup(self, c: Ctx, i: int, p: OpenDriveParams, direction: int):
        """Returns (level_column_name, reason) for the highest-priority setup that
        fires on this bar, or (None, None). Priority: retest > rejection > acceptance."""
        if direction == 1:
            if p.use_retest and c["retest_up"][i]:
                return "pvah", "retest"
            if p.use_rejection and c["reject_up"][i]:
                return "pval", "reject"
            if p.use_acceptance and c["accept_up"][i]:
                return "pvah", "accept"
        else:
            if p.use_retest and c["retest_dn"][i]:
                return "pval", "retest"
            if p.use_rejection and c["reject_dn"][i]:
                return "pvah", "reject"
            if p.use_acceptance and c["accept_dn"][i]:
                return "pval", "accept"
        return None, None

    def _target(self, c: Ctx, i: int, p: OpenDriveParams, px: float, risk: float,
                direction: int) -> float:
        cap = px + direction * risk * p.tp_r_cap
        if p.tp_mode == "r":
            return px + direction * risk * p.tp_r_fallback
        if p.tp_mode != "structural":
            raise ValueError(f"unknown tp_mode {p.tp_mode!r}")

        levels = [c[k][i] for k in ("ppoc", "pvah", "pval", "pdh", "pdl", "ibh", "ibl")]
        levels = [lv for lv in levels if np.isfinite(lv)]
        ahead = [lv for lv in levels if ((lv > px) if direction == 1 else (lv < px))]
        if not ahead:
            return px + direction * risk * p.tp_r_fallback
        nearest = min(ahead) if direction == 1 else max(ahead)
        # Cap an implausibly distant structural level rather than discard it -
        # still "structural", just not allowed to promise more than tp_r_cap R.
        return min(nearest, cap) if direction == 1 else max(nearest, cap)
