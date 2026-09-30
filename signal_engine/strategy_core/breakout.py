"""BREAKOUT key-level engine, ported from breakout.pine (klTrackBreaks .. klFireGate).

Faithful to the Pine logic, computed on BROKER data. It does not chase TradingView's
numbers: any difference in the input series is a data-source difference, not a logic bug.

Input frame (5-minute bars, IST index; one row per bar, every column causal):
    Close, High, Low        bar prices
    atr                     Wilder ATR(14) on the bar series
    vwap, ema9, ema_slope   session VWAP of close, EMA(9) of close, ema9 - ema9[1]
    rvol                    volume / per-slot bar baseline (0 where the baseline is unknown)
    sess_vf                 session volume factor
    htf_close               close of the previous completed 15-minute bucket
    pdh, pdl                previous session high / low
    ib_high, ib_low         running initial-balance extremes, frozen once the window ends
    ib_done                 True once the IB window (09:15-10:15) has closed
    orb_high, orb_low       opening-range extremes (NaN until formed)
    pvah, ppoc, pval        previous-session value area
    adr                     14-day average daily range, previous days only
    day_high, day_low       running session high / low including the current bar
    bias_ok  (optional)     counter-bias veto result, default True (day-type engine deferred)
    tradeable (optional)    caller's slot availability, default True

Output: one row per fired setup, indexed by the signal bar. Fill happens on the NEXT bar;
`entry_ref` is the signal bar's close, as in the Pine.

Deliberately reproduced Pine behaviour: klTrackBreaks stamps the break bar BEFORE
klResolveSetup runs, so the break bar itself can satisfy the retest branch, which outranks
-BRK. Break stamps also persist across sessions, as the Pine's `var` state does.

Deferred (documented, not silently dropped): the day-type / counter-bias engine (`bias_ok`
is an input), the afternoon entry window (off by default in the Pine), the gap and NR
entry filters, and the TP ladder / runner management (this module stops at the targets).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SESSION_OPEN_MIN = 9 * 60 + 15
SESSION_CLOSE_MIN = 15 * 60 + 30

# Resolution order of klResolveSetup: retest > rejection > acceptance > break, and
# value area > previous day > initial balance inside each tier. (code, direction, level column)
_SETUP_ORDER = (
    ("VAH-RT", 1, "pvah"),
    ("VAL-RT", -1, "pval"),
    ("PDH-RT", 1, "pdh"),
    ("PDL-RT", -1, "pdl"),
    ("IBH-RT", 1, "ib_high"),
    ("IBL-RT", -1, "ib_low"),
    ("VAL-REJ", 1, "pval"),
    ("VAH-REJ", -1, "pvah"),
    ("VAH-ACC", 1, "pvah"),
    ("VAL-ACC", -1, "pval"),
    ("PDH-BRK", 1, "pdh"),
    ("PDL-BRK", -1, "pdl"),
    ("IBH-BRK", 1, "ib_high"),
    ("IBL-BRK", -1, "ib_low"),
)
_REGISTRY = ("ppoc", "pvah", "pval", "pdh", "pdl", "ib_high", "ib_low", "orb_high", "orb_low")


@dataclass(frozen=True)
class BreakoutParams:
    """Defaults are the shipped breakout.pine inputs."""

    tick: float = 0.05  # broker tick (SymToken tick_size); confluence epsilon is tick / 2
    va_triggers: bool = True  # enableVATriggers
    va_acceptance: bool = False  # enableVAAcceptance
    pd_triggers: bool = True  # enablePDTriggers
    ib_triggers: bool = True  # enableIBTriggers
    acc_min_rvol: float = 1.5  # klAccMinRvol
    retest_max_bars: int = 6  # klRetestMaxBars
    cooldown_bars: int = 3  # klCooldownBars
    score_threshold: int = 7  # klScoreThreshold
    conf_atr_mult: float = 0.25  # klConfAtrMult
    sl_buffer_mult: float = 0.35  # klSlBufferMult
    t1_pad_mult: float = 0.15  # klT1PadMult
    clv_long_min: float = 0.50  # klClvLongMin
    clv_short_max: float = 0.50  # klClvShortMax
    rvol_min: float = 1.2  # volumeMultiplier
    rvol_strong: float = 1.8  # strongVolumeMultiplier
    min_session_vf: float = 0.8  # klMinSessionVF
    break_min_rvol: float = 1.5  # klBreakMinRvol
    max_chase_atr: float = 1.0  # klMaxChaseATR
    min_headroom_r: float = 1.5  # klMinHeadroomR
    max_tp_r: float = 2.0  # klMaxTpR
    require_htf_close: bool = True  # klRequireHTFClose
    min_entry_min: int = 9 * 60 + 45  # minEntryHour:Minute
    entry_cutoff_min: int = 11 * 60 + 45  # entryCutoffHour:Minute


def _col(df: pd.DataFrame, name: str) -> np.ndarray:
    return df[name].to_numpy(dtype=np.float64)


def _shift(a: np.ndarray) -> np.ndarray:
    out = np.empty_like(a)
    out[0] = np.nan
    out[1:] = a[:-1]
    return out


def _break_bars(event: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Bar index of the latest break at or before each bar (-1 if none)."""
    return np.maximum.accumulate(np.where(event, idx, -1))


def _resolve_setups(df: pd.DataFrame, p: BreakoutParams):
    """klTrackBreaks + klResolveSetup, vectorised."""
    n = len(df)
    idx = np.arange(n)
    close, high, low = _col(df, "Close"), _col(df, "High"), _col(df, "Low")
    prev = _shift(close)
    atr = np.nan_to_num(_col(df, "atr"))
    band = atr * p.conf_atr_mult

    rng = high - low
    clv = np.where(rng > 0, (close - low) / np.where(rng > 0, rng, 1.0), 0.5)
    bull, bear = clv >= p.clv_long_min, clv <= p.clv_short_max
    rvol = np.nan_to_num(_col(df, "rvol"))

    lv = {k: _col(df, k) for k in ("pvah", "pval", "pdh", "pdl", "ib_high", "ib_low")}
    # klTrackBreaks: highs break upward, lows break downward
    broke = {
        "pvah": (close > lv["pvah"]) & (prev <= lv["pvah"]),
        "pval": (close < lv["pval"]) & (prev >= lv["pval"]),
        "pdh": (close > lv["pdh"]) & (prev <= lv["pdh"]),
        "pdl": (close < lv["pdl"]) & (prev >= lv["pdl"]),
        "ib_high": (close > lv["ib_high"]) & (prev <= lv["ib_high"]),
        "ib_low": (close < lv["ib_low"]) & (prev >= lv["ib_low"]),
    }
    near = {}
    for k, ev in broke.items():
        bb = _break_bars(ev, idx)
        near[k] = (bb >= 0) & (idx - bb <= p.retest_max_bars)

    va, pd_ = p.va_triggers, p.pd_triggers
    ib = p.ib_triggers & df["ib_done"].to_numpy(dtype=bool)
    acc_vol = (p.acc_min_rvol <= 0) | (rvol >= p.acc_min_rvol)
    vah, val, pdh, pdl, ibh, ibl = (
        lv[k] for k in ("pvah", "pval", "pdh", "pdl", "ib_high", "ib_low")
    )

    cond = {
        "VAH-RT": va & near["pvah"] & (low <= vah + band) & (close > vah) & bull,
        "VAL-RT": va & near["pval"] & (high >= val - band) & (close < val) & bear,
        "PDH-RT": pd_ & near["pdh"] & (low <= pdh + band) & (close > pdh) & bull,
        "PDL-RT": pd_ & near["pdl"] & (high >= pdl - band) & (close < pdl) & bear,
        "IBH-RT": ib & near["ib_high"] & (low <= ibh + band) & (close > ibh) & bull,
        "IBL-RT": ib & near["ib_low"] & (high >= ibl - band) & (close < ibl) & bear,
        "VAL-REJ": va & (low < val) & (close > val) & bull,
        "VAH-REJ": va & (high > vah) & (close < vah) & bear,
        "VAH-ACC": va & p.va_acceptance & acc_vol & (close > vah) & (prev > vah),
        "VAL-ACC": va & p.va_acceptance & acc_vol & (close < val) & (prev < val),
        "PDH-BRK": pd_ & (close > pdh) & (prev <= pdh) & bull,
        "PDL-BRK": pd_ & (close < pdl) & (prev >= pdl) & bear,
        "IBH-BRK": ib & (close > ibh) & (prev <= ibh) & bull,
        "IBL-BRK": ib & (close < ibl) & (prev >= ibl) & bear,
    }
    code = np.full(n, "", dtype=object)
    direction = np.zeros(n, dtype=np.int64)
    level = np.full(n, np.nan)
    for name, d, col in _SETUP_ORDER:
        hit = np.asarray(cond[name], dtype=bool) & (code == "")
        code[hit], direction[hit], level[hit] = name, d, _col(df, col)[hit]
    return code, direction, level, bull, bear, rvol


def _next_level(levels: np.ndarray, d: np.ndarray, ref: np.ndarray, tick: float) -> np.ndarray:
    """klNextLevel: nearest registry level strictly beyond ref in direction d (raw, unpadded)."""
    r = ref[:, None]
    up = np.where(levels > r + tick, levels, np.inf).min(axis=1)
    dn = np.where(levels < r - tick, levels, -np.inf).max(axis=1)
    out = np.where(d == 1, up, dn)
    return np.where(np.isfinite(out), out, np.nan)


def _pad_target(lv: np.ndarray, d: np.ndarray, ref: np.ndarray, pad: np.ndarray) -> np.ndarray:
    """klPadTarget: park just short of the level; NaN when the pad leaves no workable target."""
    padded = np.where(d == 1, lv - pad, lv + pad)
    ok = np.where(d == 1, padded > ref + pad, padded < ref - pad)
    return np.where(ok & np.isfinite(padded), padded, np.nan)


def _score(d, conf, is_retest, close, vwap, ema9, slope, rvol, bull, bear, vf, p) -> np.ndarray:
    """klComputeScore."""
    long_, short_ = d == 1, d == -1
    s = 2 + np.minimum(conf, 3) + 2 * (conf >= 2) + 2 * is_retest
    s = s + ((long_ & (close > vwap)) | (short_ & (close < vwap)))
    s = s + ((long_ & (close > ema9) & (slope > 0)) | (short_ & (close < ema9) & (slope < 0)))
    s = s + np.where(rvol >= p.rvol_strong, 2, np.where(rvol >= p.rvol_min, 1, 0))
    s = s + ((long_ & bull) | (short_ & bear))
    return s + (np.isfinite(vf) & (vf >= p.rvol_min))


def compute_signals(df: pd.DataFrame, p: BreakoutParams | None = None) -> pd.DataFrame:
    """Return the fired setups for a prepared input frame (see module docstring)."""
    p = p or BreakoutParams()
    with np.errstate(invalid="ignore", divide="ignore"):
        return _compute(df, p)


def _compute(df: pd.DataFrame, p: BreakoutParams) -> pd.DataFrame:
    code, direction, level, bull, bear, rvol_bar = _resolve_setups(df, p)
    sel = np.flatnonzero(direction != 0)
    if sel.size == 0:
        return _empty()

    def at(name: str) -> np.ndarray:
        return _col(df, name)[sel]

    d, lvl, close = direction[sel], level[sel], at("Close")
    atr_raw, atr = at("atr"), np.nan_to_num(at("atr"))
    is_retest = np.array([c.endswith("-RT") for c in code[sel]])
    is_break = np.array([c.endswith("-BRK") for c in code[sel]])
    rvol = rvol_bar[sel]
    window = pd.Series(rvol_bar).rolling(p.retest_max_bars, min_periods=1).max().to_numpy()[sel]

    levels = np.column_stack([_col(df, k)[sel] for k in _REGISTRY])
    band = atr * p.conf_atr_mult
    dist = np.abs(lvl[:, None] - levels)
    eps = p.tick * 0.5
    conf = ((dist <= band[:, None]) & (dist > eps) & (band[:, None] > 0)).sum(axis=1)

    score = _score(
        d,
        conf,
        is_retest,
        close,
        at("vwap"),
        at("ema9"),
        at("ema_slope"),
        np.where(is_retest, np.maximum(rvol, window), rvol),
        bull[sel],
        bear[sel],
        at("sess_vf"),
        p,
    )

    # klExecMap: stop beyond the level, never nearer than 0.5 ATR / 0.3% from entry
    min_dist = np.maximum(atr * 0.5, close * 0.003)
    raw_sl = lvl - d * atr * p.sl_buffer_mult
    sl = np.where(
        d == 1, np.minimum(raw_sl, close - min_dist), np.maximum(raw_sl, close + min_dist)
    )
    risk = np.abs(close - sl)

    pad = atr * p.t1_pad_mult
    t1_raw = _next_level(levels, d, close, p.tick)
    t1 = _pad_target(t1_raw, d, close, pad)
    t2 = np.full_like(t1, np.nan)
    has_t1 = np.isfinite(t1_raw)
    if has_t1.any():
        beyond = _next_level(levels[has_t1], d[has_t1], t1_raw[has_t1], p.tick)
        t2[has_t1] = _pad_target(beyond, d[has_t1], close[has_t1], pad[has_t1])
    tp1, tp1_5, tp2, tp3 = _targets(close, sl, d, t1, t2)

    # gates
    vf, htf = at("sess_vf"), at("htf_close")
    risk_ok = np.isfinite(risk) & (risk > 0)
    adr, day_hi, day_lo = at("adr"), at("day_high"), at("day_low")
    proj = np.where(d == 1, day_lo + adr, day_hi - adr)
    headroom = np.where(
        risk_ok & np.isfinite(proj) & (adr > 0),
        np.where(d == 1, proj - close, close - proj) / np.where(risk_ok, risk, 1.0),
        np.nan,
    )
    room_ok = (p.min_headroom_r <= 0) | np.isnan(headroom) | (headroom >= p.min_headroom_r)
    tp_r = np.where(
        np.isfinite(t1) & risk_ok, np.abs(t1 - close) / np.where(risk_ok, risk, 1.0), np.nan
    )
    reach_ok = (p.max_tp_r <= 0) | np.isnan(tp_r) | (tp_r <= p.max_tp_r)
    chase = np.where(atr > 0, np.abs(close - lvl) / np.where(atr > 0, atr, 1.0), np.nan)
    chase_ok = (p.max_chase_atr <= 0) | np.isnan(chase) | (chase <= p.max_chase_atr)
    sess_ok = (p.min_session_vf <= 0) | np.isnan(vf) | (vf >= p.min_session_vf)
    break_vol_ok = is_break & ((p.break_min_rvol <= 0) | (rvol >= p.break_min_rvol))
    htf_ok = (
        (not p.require_htf_close)
        | np.isnan(htf)
        | np.isnan(lvl)
        | (rvol >= p.rvol_strong)
        | break_vol_ok
        | ((d == 1) & (htf > lvl))
        | ((d == -1) & (htf < lvl))
    )
    bias_ok = (
        df["bias_ok"].to_numpy(dtype=bool)[sel] if "bias_ok" in df else np.ones(sel.size, bool)
    )
    tradeable = (
        df["tradeable"].to_numpy(dtype=bool)[sel] if "tradeable" in df else np.ones(sel.size, bool)
    )
    mins = (df.index.hour * 60 + df.index.minute).to_numpy()[sel]
    in_window = (
        (mins >= p.min_entry_min)
        & (mins < p.entry_cutoff_min)
        & (mins >= SESSION_OPEN_MIN)
        & (mins < SESSION_CLOSE_MIN)
    )

    candidate = (
        (score >= p.score_threshold)
        & htf_ok
        & room_ok
        & reach_ok
        & chase_ok
        & bias_ok
        & tradeable
        & sess_ok
        & in_window
    )
    keep = _apply_cooldown(sel[candidate], p.cooldown_bars)
    chosen = np.isin(sel, keep)
    if not chosen.any():
        return _empty()

    return pd.DataFrame(
        {
            "code": code[sel][chosen],
            "direction": d[chosen],
            "level": lvl[chosen],
            "entry_ref": close[chosen],
            "sl": sl[chosen],
            "risk": risk[chosen],
            "t1": t1[chosen],
            "tp1": tp1[chosen],
            "tp1_5": tp1_5[chosen],
            "tp2": tp2[chosen],
            "tp3": tp3[chosen],
            "score": score[chosen].astype(int),
            "conf": conf[chosen],
            "chase": chase[chosen],
            "headroom": headroom[chosen],
            "tp_r": tp_r[chosen],
            "atr": atr_raw[chosen],
        },
        index=df.index[sel[chosen]],
    )


def _targets(entry, sl, d, t1, t2):
    """klCalcTargets: TP1 at the structural T1 (floored at 1R), 1.5 midway, TP2 structural."""
    risk = np.abs(entry - sl)
    dist = np.where(np.isnan(t1), risk * 1.5, np.maximum(np.abs(t1 - entry), risk))
    tp1 = entry + d * dist
    floor2 = tp1 + d * dist * 0.5
    struct2 = np.where(
        np.isnan(t2), np.nan, np.where(d == 1, np.maximum(t2, floor2), np.minimum(t2, floor2))
    )
    tp2 = np.where(np.isnan(struct2), entry + d * dist * 2.0, struct2)
    return tp1, (tp1 + tp2) / 2, tp2, entry + d * dist * 3.0


def _apply_cooldown(candidates: np.ndarray, cooldown: int) -> np.ndarray:
    """klFireGate `fresh`: a fire needs more than `cooldown` bars since the previous FIRE."""
    kept: list[int] = []
    last = None
    for pos in candidates:
        if last is None or pos - last > cooldown:
            kept.append(int(pos))
            last = pos
    return np.array(kept, dtype=np.int64)


def _empty() -> pd.DataFrame:
    cols = [
        "code",
        "direction",
        "level",
        "entry_ref",
        "sl",
        "risk",
        "t1",
        "tp1",
        "tp1_5",
        "tp2",
        "tp3",
        "score",
        "conf",
        "chase",
        "headroom",
        "tp_r",
        "atr",
    ]
    return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="Asia/Kolkata"))
