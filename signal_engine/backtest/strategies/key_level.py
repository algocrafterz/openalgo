"""breakout.pine key-level engine - backtest adapter (PD and IB families).

Source: `signal_engine/pinescripts/intraday/orb/breakout.pine`, the `kl*` engine.

WHAT IS COVERED AND WHAT IS NOT
    breakout.pine trades four level families. Three are reproducible from 5-minute bars
    and are implemented here:

        PDH / PDL   previous session's high and low           -BRK and -RT
        IBH / IBL   initial balance 09:15-10:15 high and low  -BRK and -RT
        ORH/ORM/ORL the opening range, as CONFLUENCE only     (its own triggers are the
                    separate `orb` strategy, which is what production executes)

    The fourth - PVAH / PPOC / PVAL - is NOT covered. Those come from reconstructing the
    previous session's volume profile out of 1-minute bars via `request.security_lower_tf`,
    and yfinance serves only 7 days of 1-minute history against the 60 days of 5-minute
    history everything else here uses. Reconstructing a value area from 5-minute bars
    would be a different indicator wearing the same name, so the VA family is left
    untested rather than approximated. `VAH-ACC`/`VAL-ACC`, `VAH-REJ`/`VAL-REJ` and the
    VA retests are absent from these numbers.

    Because VA levels are also missing from the confluence registry, `confCnt` here is
    computed over ORB + PD + IB only. That biases scores DOWNWARD, so at a fixed
    threshold this adapter fires on a subset of what the Pine would fire on - a
    conservative direction: the setups it does take are the higher-conviction ones.

THE SETUP LADDER (klResolveSetup, VA branches removed)
    retest outranks break, and PD outranks IB:
        PDH-RT, PDL-RT, IBH-RT, IBL-RT, PDH-BRK, PDL-BRK, IBH-BRK, IBL-BRK

    break   close crosses the level, with a directional candle (CLV >= 0.65 long)
    retest  the level broke within the last `retest_bars`, price came back inside the
            ATR band around it, and this bar closed back through it in the trade's
            direction. Nothing is waited for - the pattern has already completed.

SCORING AND GATES are ported from `klComputeScore` / `klFireGate` at their shipped
    defaults (threshold 7, cooldown 3, chase <= 1.0 ATR, session VF >= 0.8,
    headroom >= 1.5R, HTF-close confirmation with the strong-volume and break-volume
    escapes). `use_bias_veto` is the one deliberate omission: `klBiasOK` needs the
    opening-type / day-type classifier, a large amount of additional state for a veto
    that only fires on Trend and Double-Distribution days. Leaving it off admits a few
    counter-bias trades the Pine would block, which again cannot flatter the result.

RISK is `klExecMap`: stop = level -/+ 0.35 ATR, floored at max(0.5 ATR, 0.3% of price)
    from entry. Target is `klCalcTargets`: the nearest structural level beyond entry,
    padded 0.15 ATR short of it, floored at 1.0R - this path already floors at 1.0R and
    never carried the 0.8R defect the ORB path had.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class KeyLevelParams:
    # ---- which families fire -------------------------------------------
    use_pd: bool = True               # enablePDTriggers
    use_ib: bool = True               # enableIBTriggers
    use_breaks: bool = True           # -BRK setups
    use_retests: bool = True          # -RT setups

    # ---- level construction --------------------------------------------
    ib_minutes: int = 60              # klIBSession 0915-1015
    orb_minutes: int = 15             # ORB levels, confluence only

    # ---- setup detection -----------------------------------------------
    retest_bars: int = 6              # klRetestMaxBars
    #: Minimum bars between the break and the "retest". 0 is the Pine: klTrackBreaks
    #: stamps the break bar and runs BEFORE klResolveSetup, so on the break bar itself
    #: `bar_index - pdhBB == 0`, the -RT branch (checked first) matches, and the -BRK
    #: branch is never reached. Set 1 to separate a real break-and-hold from the break.
    retest_min_bars: int = 0
    conf_atr_mult: float = 0.25       # klConfAtrMult - confluence / retest band
    clv_long_min: float = 0.65        # klClvLongMin
    clv_short_max: float = 0.35       # klClvShortMax

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
    use_bias_veto: bool = False       # klBlockCounterBias, see module docstring
    min_entry_min: int = 9 * 60 + 45
    min_price: float = 20.0

    # ---- risk -----------------------------------------------------------
    atr_len: int = 14
    sl_buffer_mult: float = 0.35      # klSlBufferMult, ATR beyond the level
    t1_pad_mult: float = 0.15         # klT1PadMult, ATR short of the target level
    adr_days: int = 14

    # ---- target selection ------------------------------------------------
    #: "level" is the shipped Pine behaviour: nearest structural level beyond entry,
    #: padded, floored at 1.0R. "r" ignores structure and takes a fixed reward-to-risk,
    #: which is the "1:1 / 1:2" half of the trader's stated rule. "level_min_r" keeps
    #: the structural target but floors it at `tp_r` instead of at 1.0R.
    tp_mode: str = "level"            # level | r | level_min_r
    tp_r: float = 2.0                 # used by tp_mode "r" and "level_min_r"

    # ---- stop selection --------------------------------------------------
    #: "level" is the shipped behaviour: stop sits `sl_buffer_mult` ATR beyond the key
    #: level. "drive" is the trader's alternative: stop beyond the extreme of the
    #: initiative drive candle - the signal bar itself, which by construction is the
    #: directional break/retest bar. "wider" is "level" with the floor raised, to test
    #: whether the shipped 0.3%-of-price stop is simply too tight to survive costs.
    sl_mode: str = "level"            # level | drive | wider
    drive_buffer_mult: float = 0.15   # ATR beyond the drive candle's extreme
    min_sl_atr: float = 0.5           # floor in ATR, raised by sl_mode="wider"
    min_sl_pct_price: float = 0.003   # floor as a fraction of price

    # ---- candlestick confirmation ---------------------------------------
    #: "" keeps the shipped CLV gate only. "engulf" additionally demands a classic
    #: engulfing bar in the trade direction; "engulf_or_clv" accepts either, which is
    #: a looser reading of "candlestick confirmation".
    pattern: str = ""                 # "" | engulf | engulf_or_clv | pin

    # ---- higher-timeframe bias ------------------------------------------
    #: The trader's first rule - only trade with the daily direction. Built from the
    #: previous COMPLETED session's close against an EMA of daily closes, so nothing
    #: from the current session leaks in.
    use_daily_bias: bool = False
    daily_bias_len: int = 5


class KeyLevel(Strategy):
    name = "breakout.pine key levels (PDH/PDL + IBH/IBL, break and retest)"
    tag = "KEYLEVEL"
    pine = "signal_engine/pinescripts/intraday/orb/breakout.pine"

    # ---- preparation ---------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: KeyLevelParams) -> pd.DataFrame:
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

    def prepare_key(self, p: KeyLevelParams) -> tuple:
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

    def _resolve(self, c: Ctx, i: int, p: KeyLevelParams):
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

    def _score(self, c: Ctx, i: int, p: KeyLevelParams, dirn: int,
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

    def entry(self, c: Ctx, i: int, p: KeyLevelParams, direction: int):
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
        if p.use_daily_bias:
            b = c["bias_up"][i] if dirn == 1 else c["bias_dn"][i]
            if not (np.isfinite(b) and b > 0):
                return None

        # ---- candlestick confirmation ----------------------------------
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

        # ---- target ----------------------------------------------------
        if p.tp_mode == "r":
            dist = risk * p.tp_r
        else:
            # klCalcTargets: nearest structural level beyond entry, padded.
            # "level" floors it at 1.0R (the Pine); "level_min_r" at tp_r.
            floor_r = 1.0 if p.tp_mode == "level" else p.tp_r
            pad = atr * p.t1_pad_mult
            beyond = [x for x in self._levels(c, i)
                      if ((x > px + pad) if dirn == 1 else (x < px - pad))]
            t1 = (min(beyond) if dirn == 1 else max(beyond)) if beyond else np.nan
            if np.isfinite(t1):
                dist = max(abs(t1 - dirn * pad - px), risk * floor_r)
            else:
                dist = risk * max(1.5, floor_r)
        tp = px + dirn * dist
        return EntrySignal(direction=dirn, sl=float(sl), tp=float(tp), tag=code)
