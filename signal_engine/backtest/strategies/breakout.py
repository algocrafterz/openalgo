"""breakout.pine - backtest adapter for the LIVE "BREAKOUT" strategy.

Source: `signal_engine/pinescripts/intraday/orb/breakout.pine`. This is the strategy
that actually trades: every alert it sends is tagged "BREAKOUT" (`buildEntryAlert`,
~line 919), and `intraday-orb` was explicitly stood down (`config.yaml`, and
breakout.md's 2026-09-06 "ORB stood down" entry) so BREAKOUT runs alone. This file
exists SEPARATELY from `key_level.py` rather than importing or subclassing it,
deliberately: cross-checking `key_level.py`'s claimed defaults against the current
pine source for this adapter turned up two places where they have already drifted
from what is actually live (below). `key_level.py` is a research tool that keeps
gaining knobs (`breakout.md`'s 2026-08-30 entry added four of them); pinning what the
live BREAKOUT tag means to a file that only research code touches would let the next
research change silently redefine what this backtest reports for real money.

ORB IS NOT PART OF THIS. `enableBreakout = input.bool(false, ...)` (line 176): in the
shipped default configuration, ORB is demoted to a confluence LEVEL only and never
triggers a trade of its own - the key-level engine always owns the session's one entry
slot. This adapter therefore models the key-level engine exclusively, matching what is
actually deployed, not an ORB-plus-key-level union.

WHAT IS COVERED (ported from `key_level.py`, cross-verified against the CURRENT pine
source rather than trusted from that file's own docstring - see CORRECTIONS below):

    PDH / PDL   previous session's high and low           -BRK and -RT
    IBH / IBL   initial balance 09:15-10:15 high and low  -BRK and -RT
    ORH/ORM/ORL the opening range, as CONFLUENCE only (ORB itself never triggers,
                see above)

    The fourth family, PVAH/PPOC/PVAL, is NOT covered - reconstructing a session's
    volume profile needs 1-minute bars via `request.security_lower_tf`, and this
    harness's OpenAlgo/Historify source does not carry that lower-timeframe join for
    every symbol in the universe. Left untested rather than approximated, same as
    `key_level.py`.

THE SETUP LADDER (klResolveSetup, VA branches removed): retest outranks break, and PD
    outranks IB - PDH-RT, PDL-RT, IBH-RT, IBL-RT, PDH-BRK, PDL-BRK, IBH-BRK, IBL-BRK.

    This reproduces a confirmed, SHIPPED bug rather than fixing it: `klTrackBreaks`
    stamps the break bar and runs BEFORE `klResolveSetup`, so on the break bar itself
    `bar_index - pdhBB == 0`, the retest branch (checked first in the ladder) matches,
    and the break branch is never reached (breakout.md, 2026-08-28, "Found: the -BRK
    branch is unreachable" - confirmed live: "Zero of 1496 shipped trades were
    breaks"). `retest_min_bars=0` below is what reproduces this. A backtest that
    "fixed" the bug would no longer describe what is actually trading.

CORRECTIONS versus `key_level.py`'s claimed defaults, found by reading the CURRENT
    pine source directly rather than trusting that file's docstring (both confirmed
    at the cited line numbers in breakout.pine, current as of this port):

    1. CLV gate. `key_level.py` ships `clv_long_min=0.65, clv_short_max=0.35`, citing
       "klClvLongMin"/"klClvShortMax". The current source (lines 425-426) reads
       `klClvLongMin = 0.50`, `klClvShortMax = 0.50` - relaxed on 2026-08-30b
       (breakout.md) after the original 0.65/0.35 gate was found to be INVERTED (the
       more decisive the break candle, the WORSE the follow-through - breakout.md's
       2026-08-30 "Corollary 1"). This file uses 0.50/0.50, matching what is live.

    2. TP1 reachability gate. `key_level.py` has no equivalent of `klMaxTpR` (added
       2026-09-06, breakout.md "Finding 3"). The current source (line 437, consumed
       at line 3639: `klTpReachOK = klMaxTpR <= 0 or na(klTpR) or klTpR <= klMaxTpR`,
       ANDed into `klRoomOK` at the `klFireGate` call, line 3709) refuses - SKIPS,
       does not rescale - any setup whose nearest structural level sits more than
       `max_tp_r` (default 2.0) risk-units away. Pooled evidence: 4 of 4 such setups
       in the live sample booked nothing, 2 of them never resolved at all. This file
       adds `max_tp_r` and the gate; `key_level.py` does not have it as of this port.

    Everything else numeric here (score_threshold=7, cooldown_bars=3, conf_atr_mult=
    0.25, sl_buffer_mult=0.35, t1_pad_mult=0.15, min_session_vf=0.8,
    break_min_rvol=1.5, max_chase_atr=1.0, min_headroom_r=1.5, require_htf_close,
    htf_bars=3 i.e. klConfirmTF="15", rvol_min=1.2, rvol_strong=1.8, ib_minutes=60,
    orb_minutes=15) was individually re-checked against the current pine source for
    this port and matches `key_level.py`'s claim exactly - only the two items above
    had drifted.

DELIBERATE OMISSION, carried forward from `key_level.py` unchanged: `klBlockCounterBias`
    (line 434) defaults ON in the live pine and blocks a setup trading against the
    day's established direction on Trend / Double-Distribution days. Implementing it
    needs the opening-type / day-type classifier (`klTrendDay`, `klBiasLong`,
    `klOpenTypeS`, `klDayTypeS`), which is a large separate feature with no equivalent
    already built in this harness. Leaving it off (`use_bias_veto=False`) ADMITS a few
    counter-bias trades the live system would refuse - a conservative direction, since
    it cannot make this backtest look better than the live system would, only add
    noise the live system filters out.

EXIT MODEL: TP1 only, single SL - matching every other adapter in this codebase (ORB,
    EMA9, ...), not breakout.pine's live multi-tier ladder (TP1/TP1.5/TP2/TP3 partial
    exits at 30/35/35, plus same-day re-entry "cycles" after a stop). Two reasons,
    not one: (a) this project's own cost-sensitivity work found TP1-only outperforms
    partial-exit laddering once realistic costs are counted for other strategies, and
    every adapter here follows that convention; (b) the engine (`engine.py`) supports
    exactly one SL and one TP per trade - modelling the ladder faithfully would mean
    changing shared harness code that all 11 other strategies depend on, which is a
    separate, larger decision than porting this one strategy. The multi-tier ladder
    and re-entry cycles are not modelled and are not approximated by any single-TP
    substitute; a trade here is scored on whether it reaches TP1 (next structural
    level, floored at 1R) or stops out, full stop, the same as every other adapter.

RISK: `klExecMap` - stop = level -/+ 0.35 ATR, floored at max(0.5 ATR, 0.3% of price)
    from entry. Target: `klCalcTargets` - nearest structural level beyond entry,
    padded 0.15 ATR short of it, floored at 1.0R, refused outright past 2.0R
    (correction 2 above).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class BreakoutParams:
    # ---- which families fire -------------------------------------------
    use_pd: bool = True               # enablePDTriggers
    use_ib: bool = True               # enableIBTriggers
    use_breaks: bool = True           # -BRK setups
    use_retests: bool = True          # -RT setups

    # ---- level construction --------------------------------------------
    ib_minutes: int = 60              # klIBSession 0915-1015
    orb_minutes: int = 15             # ORB levels, confluence only (enableORB15Signals)

    # ---- setup detection -----------------------------------------------
    retest_bars: int = 6              # klRetestMaxBars
    #: 0 reproduces the shipped bug that makes -BRK unreachable - see module
    #: docstring. Set 1 to require a real bar of separation between break and retest.
    retest_min_bars: int = 0
    conf_atr_mult: float = 0.25       # klConfAtrMult - confluence / retest band
    clv_long_min: float = 0.50        # klClvLongMin - corrected, see module docstring
    clv_short_max: float = 0.50       # klClvShortMax - corrected, see module docstring

    # ---- score ----------------------------------------------------------
    score_threshold: int = 7          # klScoreThreshold
    vol_ma_len: int = 50
    rvol_min: float = 1.2             # volumeMultiplier
    rvol_strong: float = 1.8          # strongVolumeMultiplier

    # ---- gates ----------------------------------------------------------
    cooldown_bars: int = 3            # klCooldownBars
    max_chase_atr: float = 1.0        # klMaxChaseATR
    min_session_vf: float = 0.8       # klMinSessionVF
    min_headroom_r: float = 1.5       # klMinHeadroomR
    require_htf_close: bool = True    # klRequireHTFClose
    htf_bars: int = 3                 # klConfirmTF "15" = 3 x 5-minute bars
    break_min_rvol: float = 1.5       # klBreakMinRvol - skips the HTF wait
    #: klMaxTpR - refuse (skip, not rescale) a setup whose nearest structural level is
    #: more than this many R away. 0 disables. See correction 2 in the module docstring.
    max_tp_r: float = 2.0
    use_bias_veto: bool = False       # klBlockCounterBias - deliberate omission, see docstring
    min_entry_min: int = 9 * 60 + 45
    min_price: float = 20.0

    # ---- risk -----------------------------------------------------------
    atr_len: int = 14
    sl_buffer_mult: float = 0.35      # klSlBufferMult, ATR beyond the level
    t1_pad_mult: float = 0.15         # klT1PadMult, ATR short of the target level
    adr_days: int = 14

    # ---- target selection ------------------------------------------------
    #: "level" is the shipped Pine behaviour: nearest structural level beyond entry,
    #: padded, floored at 1.0R (and now capped by max_tp_r above). "r" and
    #: "level_min_r" are research variants, not present in the live pine - kept for
    #: symmetry with key_level.py's cost_sensitivity()/ablation() workflow.
    tp_mode: str = "level"            # level | r | level_min_r
    tp_r: float = 2.0                 # used by tp_mode "r" and "level_min_r"

    # ---- stop selection --------------------------------------------------
    #: "level" is the shipped behaviour. "drive"/"wider" are research variants with no
    #: equivalent in the live pine - see key_level.py, which introduced them.
    sl_mode: str = "level"            # level | drive | wider
    drive_buffer_mult: float = 0.15   # ATR beyond the drive candle's extreme
    min_sl_atr: float = 0.5           # floor in ATR, raised by sl_mode="wider"
    min_sl_pct_price: float = 0.003   # floor as a fraction of price

    # ---- candlestick confirmation ---------------------------------------
    #: "" keeps the shipped CLV gate only - the live pine has no additional pattern
    #: requirement. The other values are research variants, not live behaviour.
    pattern: str = ""                 # "" | engulf | engulf_or_clv | pin

    # ---- higher-timeframe bias ------------------------------------------
    #: Research variant, not present in the live pine (which instead has
    #: klBlockCounterBias - see the deliberate-omission note in the module docstring).
    use_daily_bias: bool = False
    daily_bias_len: int = 5


class Breakout(Strategy):
    name = "breakout.pine - live BREAKOUT tag (key-level engine, ORB as confluence only)"
    tag = "BREAKOUT"
    pine = "signal_engine/pinescripts/intraday/orb/breakout.pine"

    # ---- preparation -----------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: BreakoutParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        c, h, low_, v = d["Close"], d["High"], d["Low"], d["Volume"]

        # ---- previous session high / low -------------------------------
        d["pdh"] = day.map(h.groupby(day).max().shift(1))
        d["pdl"] = day.map(low_.groupby(day).min().shift(1))

        # ---- initial balance, frozen when the window closes ------------
        in_ib = d["from_open"] < p.ib_minutes
        d["ibh"] = day.map(h.where(in_ib).groupby(day).max())
        d["ibl"] = day.map(low_.where(in_ib).groupby(day).min())
        d["ib_done"] = (~in_ib).astype(float)

        # ---- opening range: confluence only ----------------------------
        in_or = d["from_open"] < p.orb_minutes
        orh = day.map(h.where(in_or).groupby(day).max())
        orl = day.map(low_.where(in_or).groupby(day).min())
        d["orh"], d["orl"] = orh, orl
        d["orm"] = (orh + orl) / 2

        # ---- bar metrics (klCandle) ------------------------------------
        rng = (h - low_).replace(0, np.nan)
        clv = ((c - low_) / rng).fillna(0.5)
        d["bull"] = (clv >= p.clv_long_min).astype(float)
        d["bear"] = (clv <= p.clv_short_max).astype(float)
        vol_ma = v.rolling(p.vol_ma_len, min_periods=10).mean()
        d["rvol"] = (v / vol_ma.replace(0, np.nan)).fillna(0.0)

        # ---- session volume factor (klVolFactor) -----------------------
        # Cumulative session volume so far, against the same point of prior sessions.
        cum = v.groupby(day).cumsum()
        slot = d.groupby(day).cumcount()
        wide = pd.DataFrame({"d": day.values, "s": slot.values, "c": cum.values}) \
            .pivot_table(index="d", columns="s", values="c", aggfunc="first")
        base = wide.shift(1).rolling(14, min_periods=3).mean()
        flat = base.stack(future_stack=True).rename("b").reset_index()
        merged = pd.DataFrame({"d": day.values, "s": slot.values}).merge(
            flat, on=["d", "s"], how="left")
        d["sess_vf"] = (cum / pd.Series(merged["b"].values, index=d.index)
                        .replace(0, np.nan)).fillna(1.0)

        # ---- trend context ---------------------------------------------
        d["vwap"] = ind.session_vwap(d, day)
        ema9 = ind.ema(c, 9)
        d["ema9"] = ema9
        d["ema_slope"] = ema9 - ema9.shift(1)
        d["atr"] = ind.atr(d, p.atr_len)

        # ---- ADR and running day extremes, for the headroom gate -------
        daily_rng = (h.groupby(day).max() - low_.groupby(day).min())
        d["adr"] = day.map(daily_rng.rolling(p.adr_days, min_periods=3).mean().shift(1))
        d["day_hi"] = h.groupby(day).cummax()
        d["day_lo"] = low_.groupby(day).cummin()

        # ---- HTF confirmation close (klConfirmTF, close[1], lookahead off)
        d["htf_close"] = c.shift(p.htf_bars)

        # ---- bars since each level last broke (klTrackBreaks) ----------
        idx = pd.Series(np.arange(len(d)), index=d.index)
        for name, up in (("pdh", True), ("pdl", False), ("ibh", True), ("ibl", False)):
            lvl = d[name]
            crossed = (((c > lvl) & (c.shift(1) <= lvl)) if up
                       else ((c < lvl) & (c.shift(1) >= lvl))).fillna(False)
            # reset per session: a break is only relevant within its own day
            last = idx.where(crossed).groupby(day).ffill()
            d[f"{name}_since"] = (idx - last).fillna(9999)

        # ---- candlestick patterns (classic definitions, on the signal bar) ------
        o = d["Open"]
        body = (c - o).abs()
        prev_body = body.shift(1)
        bull_body, bear_body = c > o, c < o
        # Engulfing: this bar's body fully covers the prior body, opposite colour.
        d["engulf_bull"] = (bull_body & bear_body.shift(1)
                            & (c >= o.shift(1)) & (o <= c.shift(1))
                            & (body > prev_body)).astype(float)
        d["engulf_bear"] = (bear_body & bull_body.shift(1)
                            & (c <= o.shift(1)) & (o >= c.shift(1))
                            & (body > prev_body)).astype(float)
        # Pin bar / hammer: small body at one end, long wick from the other.
        upper = h - c.where(bull_body, o)
        lower = c.where(bear_body, o) - low_
        d["pin_bull"] = ((lower >= 2 * body) & (upper <= body)).astype(float)
        d["pin_bear"] = ((upper >= 2 * body) & (lower <= body)).astype(float)

        # ---- daily (HTF) bias from COMPLETED sessions only ---------------------
        dc = c.groupby(day).last()
        dbias = dc.ewm(span=p.daily_bias_len, adjust=False).mean()
        up = (dc > dbias).shift(1)
        d["bias_up"] = day.map(up).astype(float)
        d["bias_dn"] = day.map((~up.astype(bool)) & up.notna()).astype(float)
        return d

    def prepare_key(self, p: BreakoutParams) -> tuple:
        return (p.ib_minutes, p.orb_minutes, p.clv_long_min, p.clv_short_max,
                p.vol_ma_len, p.atr_len, p.adr_days, p.htf_bars, p.daily_bias_len)

    # ---- state ---------------------------------------------------------

    def reset_symbol(self, p) -> None:
        self._last_fire = -10_000

    def reset_session(self, p) -> None:
        self._last_fire = -10_000

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._last_fire = i

    # ---- setup resolution ----------------------------------------------

    def _resolve(self, c: Ctx, i: int, p: BreakoutParams):
        """klResolveSetup with the VA branches removed. Returns (code, dir, level)."""
        px, prev = c["Close"][i], c["Close"][i - 1]
        band = c["atr"][i] * p.conf_atr_mult
        bull, bear = c["bull"][i] > 0, c["bear"][i] > 0
        hi, lo = c["High"][i], c["Low"][i]
        ib_ok = c["ib_done"][i] > 0

        def rt(name, up):
            lvl = c[name][i]
            since = c[f"{name}_since"][i]
            if not np.isfinite(lvl) or since > p.retest_bars or since < p.retest_min_bars:
                return False
            return ((lo <= lvl + band and px > lvl and bull) if up
                    else (hi >= lvl - band and px < lvl and bear))

        def brk(name, up):
            lvl = c[name][i]
            if not np.isfinite(lvl):
                return False
            return ((px > lvl and prev <= lvl and bull) if up
                    else (px < lvl and prev >= lvl and bear))

        ladder = []
        if p.use_retests:
            if p.use_pd:
                ladder += [("PDH-RT", 1, "pdh", rt("pdh", True)),
                           ("PDL-RT", -1, "pdl", rt("pdl", False))]
            if p.use_ib and ib_ok:
                ladder += [("IBH-RT", 1, "ibh", rt("ibh", True)),
                           ("IBL-RT", -1, "ibl", rt("ibl", False))]
        if p.use_breaks:
            if p.use_pd:
                ladder += [("PDH-BRK", 1, "pdh", brk("pdh", True)),
                           ("PDL-BRK", -1, "pdl", brk("pdl", False))]
            if p.use_ib and ib_ok:
                ladder += [("IBH-BRK", 1, "ibh", brk("ibh", True)),
                           ("IBL-BRK", -1, "ibl", brk("ibl", False))]
        for code, dirn, name, fires in ladder:
            if fires:
                return code, dirn, float(c[name][i])
        return "", 0, np.nan

    def _levels(self, c: Ctx, i: int) -> list[float]:
        out = []
        for k in ("pdh", "pdl", "ibh", "ibl", "orh", "orm", "orl"):
            x = c[k][i]
            if np.isfinite(x):
                out.append(float(x))
        return out

    def _score(self, c: Ctx, i: int, p: BreakoutParams, dirn: int,
               conf: int, is_rt: bool, rvol: float) -> int:
        s = 2
        s += min(conf, 3)
        if conf >= 2:
            s += 2
        if is_rt:
            s += 2
        vw = c["vwap"][i]
        if np.isfinite(vw) and ((dirn == 1 and c["Close"][i] > vw)
                                or (dirn == -1 and c["Close"][i] < vw)):
            s += 1
        e, sl = c["ema9"][i], c["ema_slope"][i]
        if np.isfinite(e) and ((dirn == 1 and c["Close"][i] > e and sl > 0)
                               or (dirn == -1 and c["Close"][i] < e and sl < 0)):
            s += 1
        if rvol >= p.rvol_strong:
            s += 2
        elif rvol >= p.rvol_min:
            s += 1
        if (dirn == 1 and c["bull"][i] > 0) or (dirn == -1 and c["bear"][i] > 0):
            s += 1
        vf = c["sess_vf"][i]
        if np.isfinite(vf) and vf >= p.rvol_min:
            s += 1
        return s

    # ---- entry ---------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: BreakoutParams, direction: int):
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

        code, dirn, lvl = self._resolve(c, i, p)
        if dirn != direction or not np.isfinite(lvl):
            return None
        is_rt = code.endswith("-RT")
        is_brk = code.endswith("-BRK")

        # ---- rule 1: trade only with the higher-timeframe bias ---------
        # (research variant - not present in the live pine, see module docstring)
        if p.use_daily_bias:
            b = c["bias_up"][i] if dirn == 1 else c["bias_dn"][i]
            if not (np.isfinite(b) and b > 0):
                return None

        # ---- candlestick confirmation (research variant, see docstring) -
        if p.pattern:
            eng = c["engulf_bull"][i] if dirn == 1 else c["engulf_bear"][i]
            pin = c["pin_bull"][i] if dirn == 1 else c["pin_bear"][i]
            clv_ok = (c["bull"][i] if dirn == 1 else c["bear"][i]) > 0
            if p.pattern == "engulf" and not eng > 0:
                return None
            if p.pattern == "pin" and not pin > 0:
                return None
            if p.pattern == "engulf_or_clv" and not (eng > 0 or clv_ok):
                return None

        # a retest is scored on the best bar of its window, not the quiet confirming
        # bar (klWindowRvol)
        rvol = c["rvol"][i]
        if is_rt:
            lo_i = max(0, i - p.retest_bars + 1)
            rvol = float(np.nanmax(c["rvol"][lo_i:i + 1]))

        # ---- gates -----------------------------------------------------
        if p.max_chase_atr > 0 and abs(px - lvl) / atr > p.max_chase_atr:   # klChaseOK
            return None
        vf = c["sess_vf"][i]
        if p.min_session_vf > 0 and np.isfinite(vf) and vf < p.min_session_vf:
            return None
        if p.require_htf_close:                                             # klHTFOK
            hc = c["htf_close"][i]
            strong = rvol >= p.rvol_strong
            break_vol = is_brk and rvol >= p.break_min_rvol
            beyond = np.isfinite(hc) and ((dirn == 1 and hc > lvl)
                                          or (dirn == -1 and hc < lvl))
            if not (strong or break_vol or beyond or not np.isfinite(hc)):
                return None

        band = atr * p.conf_atr_mult
        eps = 1e-9
        conf = sum(1 for x in self._levels(c, i) if eps < abs(lvl - x) <= band)
        if self._score(c, i, p, dirn, conf, is_rt, rvol) < p.score_threshold:
            return None

        # ---- stop: klExecMap by default, floored from entry ------------
        if p.sl_mode == "drive":
            # beyond the extreme of the initiative drive candle (this signal bar)
            ext = c["Low"][i] if dirn == 1 else c["High"][i]
            raw = ext - dirn * atr * p.drive_buffer_mult
        else:
            raw = lvl - dirn * atr * p.sl_buffer_mult
        min_dist = max(atr * p.min_sl_atr, px * p.min_sl_pct_price)
        sl = min(raw, px - min_dist) if dirn == 1 else max(raw, px + min_dist)
        risk = abs(px - sl)
        if risk <= 0 or not np.isfinite(risk):
            return None

        # ---- headroom against the ADR projection (klHeadroomR) ---------
        adr, dh, dl = c["adr"][i], c["day_hi"][i], c["day_lo"][i]
        if p.min_headroom_r > 0 and np.isfinite(adr) and adr > 0:
            proj = (dl + adr) if dirn == 1 else (dh - adr)
            room = (proj - px if dirn == 1 else px - proj) / risk
            if room < p.min_headroom_r:
                return None

        # ---- target: klCalcTargets, nearest structural level beyond entry ----
        pad = atr * p.t1_pad_mult
        beyond = [x for x in self._levels(c, i)
                  if ((x > px + pad) if dirn == 1 else (x < px - pad))]
        t1 = (min(beyond) if dirn == 1 else max(beyond)) if beyond else np.nan

        # ---- klMaxTpR: refuse (skip, not rescale) an unreachable TP1 ---------
        # klTpReachOK = klMaxTpR <= 0 or na(klTpR) or klTpR <= klMaxTpR (breakout.pine
        # line 3639). klTpR is only defined when a structural level exists, so an
        # na() t1 always passes - key_level.py's "no level ahead" 1.5R fallback below
        # is unaffected by this gate, matching the live pine.
        if p.max_tp_r > 0 and np.isfinite(t1):
            tp_r = abs(t1 - dirn * pad - px) / risk
            if tp_r > p.max_tp_r:
                return None

        if p.tp_mode == "r":
            dist = risk * p.tp_r
        else:
            # "level" floors it at 1.0R (the Pine); "level_min_r" at tp_r.
            floor_r = 1.0 if p.tp_mode == "level" else p.tp_r
            if np.isfinite(t1):
                dist = max(abs(t1 - dirn * pad - px), risk * floor_r)
            else:
                dist = risk * max(1.5, floor_r)
        tp = px + dirn * dist
        return EntrySignal(direction=dirn, sl=float(sl), tp=float(tp), tag=code)
