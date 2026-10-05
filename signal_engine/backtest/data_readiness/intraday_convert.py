"""Intraday 1m rules: cleanup, session summary, price-basis conversion, NSE cross-check.

Every constant and every rule here is part of the intraday dataset contract
(INTRADAY-5M-DATASET-PLAN.md sections 4, 6 and 7, conversion_spec_version "1"). A change to any
of them is a new spec version and a new dataset version; nothing here is tuned per strategy.

Inputs are plain DataFrames so each rule can be tested on synthetic bars:
    bars  symbol, timestamp (epoch s), open, high, low, close, volume
    ref   symbol, session, raw_open, raw_high, raw_low, raw_volume, price_factor,
          volume_factor, ref_close (NSE `last` before the closing auction, official close after)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

CONVERSION_SPEC_VERSION = "1"
AGREE = 0.001                 # two ratios agree when |a / b - 1| <= AGREE
K_MAX = 1.01                  # a vendor back-adjustment never raises a past price
FALLBACK_PAIRS = (("open", "close"), ("high", "open"), ("high", "close"), ("low", "open"),
                  ("low", "close"))
CONTAIN_REL, CONTAIN_ABS = 0.0005, 0.05
FALLBACK_REACH = 0.01         # an unused extreme lies within 1% of the NSE extreme
VOL_LO, VOL_HI = 0.85, 1.02
OPEN_TOL = 0.005              # reported only
SESSION_OPEN_MIN = 9 * 60 + 15

_REPO = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = _REPO / "signal_engine" / "research" / "reference" / "nse_session_template.yaml"

CONVERTED = "converted"
CONVERTED_FALLBACK = "converted_fallback"
UNRESOLVED = "unresolved"
PUBLISHABLE = (CONVERTED, CONVERTED_FALLBACK)


# ---------------------------------------------------------------- session template

@dataclass(frozen=True)
class Template:
    start: date
    end: date | None
    windows: tuple[tuple[int, int], ...]   # continuous windows, [first, last] minute of day
    auction: tuple[int, ...]               # minute stamps of the closing-auction print

    @property
    def minutes(self) -> int:
        return sum(b - a + 1 for a, b in self.windows)


def _minute(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def load_templates(path: Path = TEMPLATE_PATH) -> tuple[Template, ...]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return tuple(Template(start=t["from"], end=t["to"],
                          windows=tuple((_minute(a), _minute(b)) for a, b in t["continuous"]),
                          auction=tuple(_minute(x) for x in t["closing_auction"] or ()))
                 for t in doc["templates"])


def template_for(day: date, templates: tuple[Template, ...]) -> Template:
    for t in templates:
        if t.start <= day and (t.end is None or day <= t.end):
            return t
    raise ValueError(f"no session template covers {day}")


def _template_index(sessions: pd.Series, templates: tuple[Template, ...]) -> pd.Series:
    """Per row: the index of its session's template."""
    uniq = {d: templates.index(template_for(pd.Timestamp(d).date(), templates))
            for d in pd.unique(sessions)}
    return sessions.map(uniq)


# ---------------------------------------------------------------- cleanup (section 4)

def with_clock(bars: pd.DataFrame) -> pd.DataFrame:
    """Add the IST session date, the minute of day and the second within the minute."""
    ist = pd.to_datetime(bars.timestamp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    return bars.assign(session=ist.dt.tz_localize(None).dt.normalize(),
                       minute=ist.dt.hour * 60 + ist.dt.minute, second=ist.dt.second)


def clean(bars: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """C1 off-minute and C2 pre-09:15 bars are removed; C3 negative volume flags the session.

    Returns (kept bars, removed bars with their rule, per-session flags)."""
    b = bars if "minute" in bars else with_clock(bars)
    off = b.second != 0
    pre = ~off & (b.minute < SESSION_OPEN_MIN)
    removed = pd.concat([b[off].assign(rule="C1_off_minute"), b[pre].assign(rule="C2_pre_open")])
    kept = b[~off & ~pre]
    flags = b.assign(contaminated=off | pre, negative_volume=b.volume < 0).groupby(
        ["symbol", "session"]).agg(contaminated=("contaminated", "any"),
                                   negative_volume=("negative_volume", "any"))
    return kept.reset_index(drop=True), removed.reset_index(drop=True), flags.reset_index()


# ---------------------------------------------------------------- session summary

def summarise(bars: pd.DataFrame, templates: tuple[Template, ...]) -> pd.DataFrame:
    """One row per (symbol, session): O, H, L, the reference close C, V, and structure counts.

    C is the last continuous bar's close before the closing auction and the auction print's
    close from it. `missing_minutes` counts continuous-window minutes without a bar."""
    b = bars if "minute" in bars else with_clock(bars)
    b = b.sort_values(["symbol", "timestamp"], kind="stable")
    t_index = _template_index(b.session, templates)
    in_cont = pd.Series(False, index=b.index)
    in_auction = pd.Series(False, index=b.index)
    for i, t in enumerate(templates):
        mine = t_index == i
        for a, z in t.windows:
            in_cont |= mine & (b.minute >= a) & (b.minute <= z)
        if t.auction:
            in_auction |= mine & b.minute.isin(t.auction)
    in_cont &= b.second == 0
    in_auction &= b.second == 0
    b = b.assign(t_minutes=t_index.map(lambda i: templates[i].minutes),
                 has_auction=t_index.map(lambda i: bool(templates[i].auction)))
    prices = b[["open", "high", "low", "close"]]
    b = b.assign(in_cont=in_cont, in_auction=in_auction, outside=~in_cont & ~in_auction,
                 bad_ohlc=(b.high < prices.max(axis=1)) | (b.low > prices.min(axis=1)),
                 nonpos=(prices <= 0).any(axis=1), negvol=b.volume < 0)
    keys = ["symbol", "session"]
    s = b.groupby(keys, sort=True).agg(
        O=("open", "first"), H=("high", "max"), L=("low", "min"), V=("volume", "sum"),
        n_bars=("timestamp", "size"), n_distinct=("timestamp", "nunique"),
        outside=("outside", "sum"), bad_ohlc=("bad_ohlc", "sum"), nonpos=("nonpos", "sum"),
        negvol=("negvol", "sum"), t_minutes=("t_minutes", "first"),
        has_auction=("has_auction", "first"))
    cont = b[b.in_cont].groupby(keys)
    auc = b[b.in_auction].groupby(keys)
    s["C_cont"] = cont.close.last()
    s["n_cont_minutes"] = cont.minute.nunique()
    s["C_auction"] = auc.close.last()
    s["n_auction"] = auc.size()
    s = s.fillna({"n_cont_minutes": 0, "n_auction": 0})
    s["C"] = s.C_auction.where(s.has_auction, s.C_cont)
    s["missing_minutes"] = (s.t_minutes - s.n_cont_minutes).astype(int)
    s["duplicates"] = s.n_bars - s.n_distinct
    return s.reset_index()


def structure_ok(summary: pd.DataFrame) -> pd.Series:
    """Section 7.2 per session: no duplicate, nothing outside the template windows, no bad OHLC,
    no non-positive price, no negative volume, at most one auction print."""
    return ((summary.duplicates == 0) & (summary.outside == 0) & (summary.bad_ohlc == 0)
            & (summary.nonpos == 0) & (summary.negvol == 0) & (summary.n_auction <= 1))


# ---------------------------------------------------------------- conversion (section 6)

def _agree(a, b) -> pd.Series:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (a / b - 1).abs() <= AGREE


def convert_sessions(summary: pd.DataFrame, ref: pd.DataFrame) -> pd.DataFrame:
    """The conversion ledger: k, method, volume candidate c, every rule result and the status.

    `summary` comes from `summarise`; `ref` holds the NSE values of the same (symbol, session).
    The status is the first failing rule, in the order of the plan (6.2, 6.3, 6.4, 7.1)."""
    d = summary.merge(ref, on=["symbol", "session"], how="left", validate="one_to_one")
    r = {"high": d.H / d.raw_high, "low": d.L / d.raw_low, "open": d.O / d.raw_open,
         "close": d.C / d.ref_close}
    k = pd.Series(np.nan, index=d.index)
    method = pd.Series(None, index=d.index, dtype=object)
    prim = _agree(r["high"], r["low"])
    k[prim] = ((r["high"] + r["low"]) / 2)[prim]
    method[prim] = "high+low"
    for a, b in FALLBACK_PAIRS:
        take = method.isna() & _agree(r[a], r[b])
        k[take] = ((r[a] + r[b]) / 2)[take]
        method[take] = f"fallback:{a}+{b}"
    d["k"], d["method"] = k, method

    pf = d.price_factor
    adj_h, adj_l = d.raw_high * pf, d.raw_low * pf
    conv_h, conv_l = d.H / k * pf, d.L / k * pf
    d["contained"] = (conv_h <= adj_h * (1 + CONTAIN_REL) + CONTAIN_ABS) & (
        conv_l >= adj_l * (1 - CONTAIN_REL) - CONTAIN_ABS)
    fb = method.fillna("").str.startswith("fallback:")
    high_free = fb & ~method.fillna("").str.contains("high")
    low_free = fb & ~method.fillna("").str.contains("low")
    d["fallback_reach_ok"] = ~((high_free & (conv_h < adj_h * (1 - FALLBACK_REACH)))
                               | (low_free & (conv_l > adj_l * (1 + FALLBACK_REACH))))

    vr = (d.V / d.raw_volume).to_numpy(dtype=float)
    kk = k.to_numpy(dtype=float)
    cands = np.column_stack([np.ones(len(d)), 1 / kk, kk])
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = vr[:, None] / cands
    pick = np.abs(np.nan_to_num(ratios, nan=np.inf) - 1).argmin(axis=1)
    rows = np.arange(len(d))
    d["vol_c"], d["vol_ratio"] = cands[rows, pick], ratios[rows, pick]
    d["volume_ok"] = (d.vol_ratio >= VOL_LO) & (d.vol_ratio <= VOL_HI)
    d["close_ok"] = _agree(d.C / k, d.ref_close)
    d["open_ok"] = (d.O / k / d.raw_open - 1).abs() <= OPEN_TOL

    status = pd.Series(np.where(fb, CONVERTED_FALLBACK, CONVERTED), index=d.index, dtype=object)
    checks = [
        (d.raw_high.isna() | d.ref_close.isna(), "excluded:no_nse_row"),
        (method.isna(), UNRESOLVED),
        (~((k > 0) & (k <= K_MAX)), "excluded:k_bound"),
        (~d.contained, "excluded:range_not_contained"),
        (~d.fallback_reach_ok, "excluded:fallback_unconfirmed"),
        (~d.volume_ok, "excluded:volume"),
        (~d.close_ok, "excluded:close"),
    ]
    decided = pd.Series(False, index=d.index)
    for mask, label in checks:
        hit = mask.fillna(True).astype(bool) & ~decided
        status[hit] = label
        decided |= hit
    d["status"] = status
    return d


# ---------------------------------------------------------------- bar conversion

def convert_bars(bars: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    """Canonical bars for the sessions whose ledger status is publishable.

    price: angel / k * price_factor; volume: angel / c / volume_factor (no rounding)."""
    ok = ledger[ledger.status.isin(PUBLISHABLE)][
        ["symbol", "session", "k", "price_factor", "vol_c", "volume_factor"]]
    b = bars if "session" in bars else with_clock(bars)
    b = b.merge(ok, on=["symbol", "session"], how="inner", validate="many_to_one")
    scale = b.price_factor / b.k
    out = b[["symbol", "session", "timestamp"]].copy()
    for col in ("open", "high", "low", "close"):
        out[col] = b[col] * scale
    out["volume"] = b.volume / b.vol_c / b.volume_factor
    return out.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)
