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
    tradeable (optional)    caller's slot availability, default True

Output: one row per fired setup, indexed by the signal bar. Fill happens on the NEXT bar;
`entry_ref` is the signal bar's close, as in the Pine.

Deliberately reproduced Pine behaviour: klTrackBreaks stamps the break bar BEFORE
klResolveSetup runs, so the break bar itself can satisfy the retest branch, which outranks
-BRK. Break stamps are cleared at the first bar of each session (spec v1.0 item 5.1, owner
decision 2026-10-08; Pine's `var` state carries them, and `break_stamp_carries_across_sessions=True`
restores that). A carried stamp could pair a break of session D with the rolled levels of D+1, and
it never changed a v1.0 entry: 99,116 backtest trades, zero changed (checked 2026-10-08).

Day type and the counter-bias veto are computed here from the initial balance (spec v1.0 items 6
and 7); there is no `bias_ok` input any more.

Deferred (documented, not silently dropped): the afternoon entry window (off by default in the Pine), the gap and NR
entry filters, and the TP ladder / runner management (this module stops at the targets).
"""

from __future__ import annotations

import dataclasses
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


#: klExecMap: the stop is never nearer than this many ATR or this fraction of the signal close
#: (spec item 11, `sl_min_atr_mult` and `sl_min_pct`). Fixed here; the spec loader checks them.
SL_MIN_ATR_MULT = 0.5
SL_MIN_PCT = 0.003


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
    #: Spec v1.0 item 5.1: False clears every break stamp at the first bar of each session.
    break_stamp_carries_across_sessions: bool = False
    #: Spec v1.0 item 7: veto a setup that runs against a trend or double-distribution day.
    counter_bias_veto: bool = True
    trend_ext_pct: float = 100.0  # extension past the initial balance that makes a trend-type day
    two_sided_ext_pct: float = 15.0  # extension both ways that makes a day two-sided (never vetoed)

    @classmethod
    def from_spec(cls, core: dict) -> BreakoutParams:
        """The params of the frozen specification's `core` block (spec item 0.3).

        Every key must be consumed: a key this class cannot honour raises instead of being ignored, so a
        spec edit can never silently change nothing.
        """
        fields = {f.name for f in dataclasses.fields(cls)} - {"tick"}
        kwargs: dict = {}
        for key, value in core.items():
            if key in fields:
                kwargs[key] = value
            elif key not in _SPEC_HANDLED:
                raise ValueError(f"spec core key {key!r} is not a BreakoutParams field")
        if core.get("retest_includes_break_bar") is not True:
            raise ValueError("v1.0 freezes retest_includes_break_bar: true (the corrected rule is v1.1)")
        for key in ("afternoon_window", "gap_filter", "nr_filter"):
            if core.get(key) is not None:
                raise ValueError(f"spec core {key} is not part of v1.0 and the core cannot honour it")
        if core.get("sl_min_atr_mult", SL_MIN_ATR_MULT) != SL_MIN_ATR_MULT or (
            core.get("sl_min_pct", SL_MIN_PCT) != SL_MIN_PCT
        ):
            raise ValueError("spec sl_min_atr_mult and sl_min_pct differ from the core's fixed stop floor")
        window = core.get("entry_window")
        if window:
            kwargs["min_entry_min"] = _hhmm_to_min(window[0])
            kwargs["entry_cutoff_min"] = _hhmm_to_min(window[1])
        veto = core.get("counter_bias_veto")
        if veto:
            kwargs["counter_bias_veto"] = bool(veto["enabled"])
            kwargs["trend_ext_pct"] = float(veto["trend_ext_pct"])
            kwargs["two_sided_ext_pct"] = float(veto["two_sided_ext_pct"])
        return cls(**kwargs)


#: Spec core keys that `from_spec` handles itself rather than copying into a field.
_SPEC_HANDLED = frozenset(
    {
        "retest_includes_break_bar",
        "entry_window",
        "afternoon_window",
        "counter_bias_veto",
        "gap_filter",
        "nr_filter",
        "sl_min_atr_mult",
        "sl_min_pct",
        "htf_minutes",  # an input of build_breakout_inputs; a test compares it with the spec
    }
)


def _hhmm_to_min(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _col(df: pd.DataFrame, name: str) -> np.ndarray:
    return df[name].to_numpy(dtype=np.float64)


def _shift(a: np.ndarray) -> np.ndarray:
    out = np.empty_like(a)
    out[0] = np.nan
    out[1:] = a[:-1]
    return out


def _break_bars(event: np.ndarray, idx: np.ndarray, session_start: np.ndarray | None = None) -> np.ndarray:
    """Bar index of the latest break at or before each bar (-1 if none).

    With `session_start` (the bar index of each bar's session start) a break from an earlier session
    is forgotten: the stamp is valid only inside the session that made it.
    """
    stamp = np.maximum.accumulate(np.where(event, idx, -1))
    if session_start is None:
        return stamp
    return np.where(stamp >= session_start, stamp, -1)


_NARROW_IB_ADR_PCT = 35.0
_WIDE_IB_ADR_PCT = 60.0
_NORMAL_VARIATION_EXT_PCT = 50.0
_NON_TREND_EXT_PCT = 10.0
TREND_DAY_TYPES = ("Trend", "Double Distribution")


def _day_type(df: pd.DataFrame, p: BreakoutParams) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Day type, ext_up and ext_dn for every bar (spec v1.0 item 6, port of breakout.pine 2468-2485).

    Causal: each bar sees only the session's bars up to and including itself. Before `ib_done`, or
    when the initial balance has no range, the type is "-" and both extensions are 0. The running
    maxima start at 0 and reset at each session start.
    """
    hi, lo = _col(df, "High"), _col(df, "Low")
    ibh, ibl, adr = _col(df, "ib_high"), _col(df, "ib_low"), _col(df, "adr")
    done = df["ib_done"].to_numpy(dtype=bool)
    ib_range = ibh - ibl
    valid = done & np.isfinite(ib_range) & (ib_range > 0)
    safe = np.where(valid, ib_range, 1.0)
    up = np.where(valid, np.maximum((hi - ibh) / safe * 100.0, 0.0), 0.0)
    dn = np.where(valid, np.maximum((ibl - lo) / safe * 100.0, 0.0), 0.0)
    session = np.asarray(df.index.date)
    ext_up = pd.Series(up).groupby(session).cummax().to_numpy()
    ext_dn = pd.Series(dn).groupby(session).cummax().to_numpy()

    mx = np.maximum(ext_up, ext_dn)
    two_sided = (ext_up > p.two_sided_ext_pct) & (ext_dn > p.two_sided_ext_pct)
    with np.errstate(invalid="ignore", divide="ignore"):
        ib_adr = np.where(np.isfinite(adr) & (adr > 0), safe / np.where(adr > 0, adr, 1.0) * 100.0, np.nan)
    narrow = ib_adr < _NARROW_IB_ADR_PCT  # a missing adr makes narrow and wide false
    wide = ib_adr > _WIDE_IB_ADR_PCT
    label = np.select(
        [
            ~valid,
            two_sided,
            (mx > p.trend_ext_pct) & narrow,
            mx > p.trend_ext_pct,
            mx > _NORMAL_VARIATION_EXT_PCT,
            (mx < _NON_TREND_EXT_PCT) & narrow,
            mx < _NON_TREND_EXT_PCT,
            wide,
        ],
        ["-", "Neutral", "Trend", "Double Distribution", "Normal Variation", "Non-Trend", "Normal", "Normal"],
        default="Normal Variation",
    )
    return label.astype(object), ext_up, ext_dn


def _counter_bias_ok(label: np.ndarray, ext_up: np.ndarray, ext_dn: np.ndarray, d: np.ndarray) -> np.ndarray:
    """True where the setup passes the veto (spec item 7). A tie in extension counts as short bias."""
    trend_day = np.isin(label, TREND_DAY_TYPES)
    bias_long = ext_up > ext_dn
    return ~(trend_day & ((d == 1) != bias_long))


def _session_start(df: pd.DataFrame) -> np.ndarray:
    """Bar index of the first bar of each bar's session (the frame is a run of whole sessions)."""
    n = len(df)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    day = np.asarray(df.index.date)
    first = np.r_[True, day[1:] != day[:-1]]
    return np.maximum.accumulate(np.where(first, np.arange(n), 0))


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
    start = None if p.break_stamp_carries_across_sessions else _session_start(df)
    for k, ev in broke.items():
        bb = _break_bars(ev, idx, start)
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
    min_dist = np.maximum(atr * SL_MIN_ATR_MULT, close * SL_MIN_PCT)
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
    day_label, ext_up, ext_dn = _day_type(df, p)
    day_label, ext_up, ext_dn = day_label[sel], ext_up[sel], ext_dn[sel]
    bias_ok = (
        _counter_bias_ok(day_label, ext_up, ext_dn, d)
        if p.counter_bias_veto
        else np.ones(sel.size, bool)
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
            "day_type": day_label[chosen],
            "ext_up": ext_up[chosen],
            "ext_dn": ext_dn[chosen],
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
