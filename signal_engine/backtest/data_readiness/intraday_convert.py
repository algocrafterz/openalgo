"""Intraday 1m rules: cleanup, session summary, price-basis conversion, NSE cross-check.

Every constant and every rule here is part of the intraday dataset contract
(INTRADAY-5M-DATASET-PLAN.md sections 4, 6 and 7, conversion_spec_version "1"). A change to any
of them is a new spec version and a new dataset version; nothing here is tuned per strategy.

Inputs are plain DataFrames so each rule can be tested on synthetic bars:
    bars  symbol, timestamp (epoch s), open, high, low, close, volume
    ref   symbol, session, raw_open, raw_high, raw_low, raw_close (NSE official close),
          raw_volume, price_factor, volume_factor, ref_close (NSE `last` before the closing
          auction, the official close from it)

Thresholds that are policy assumptions rather than definitions are read from the versioned
research data-quality policy (data_quality_policy.yaml); its sha256 is `policy_version()`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

_REPO = Path(__file__).resolve().parents[3]
_REFERENCE = _REPO / "signal_engine" / "research" / "reference"
TEMPLATE_PATH = _REFERENCE / "nse_session_template.yaml"
POLICY_PATH = _REFERENCE / "data_quality_policy.yaml"


def load_policy(path: Path = POLICY_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def policy_version(path: Path = POLICY_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


POLICY = load_policy()

CONVERSION_SPEC_VERSION = "1"
AGREE = 0.001                 # two ratios agree when |a / b - 1| <= AGREE
K_MAX = 1.01                  # a vendor back-adjustment never raises a past price
FALLBACK_PAIRS = (("open", "close"), ("high", "open"), ("high", "close"), ("low", "open"),
                  ("low", "close"))
CONTAIN_REL, CONTAIN_ABS = 0.0005, 0.05
FALLBACK_REACH = POLICY["intraday"]["fallback_reach"]   # policy assumption, not a fact
VOL_LO, VOL_HI = 0.85, 1.02
OPEN_TOL = 0.005              # reported only
SESSION_OPEN_MIN = 9 * 60 + 15
EDGE_MINUTES = POLICY["intraday"]["partial_session"]["edge_minutes"]
FIELD_FAMILIES = tuple(POLICY["field_families"])
CONVENTIONS = ("official_close", "last_trade", "both", "none")

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
                 t_open=t_index.map(lambda i: templates[i].windows[0][0]),
                 t_close=t_index.map(lambda i: templates[i].windows[-1][1]),
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
        t_open=("t_open", "first"), t_close=("t_close", "first"),
        has_auction=("has_auction", "first"))
    cont = b[b.in_cont].groupby(keys)
    auc = b[b.in_auction].groupby(keys)
    s["C_cont"] = cont.close.last()
    s["n_cont_minutes"] = cont.minute.nunique()
    s["first_cont"] = cont.minute.min()
    s["last_cont"] = cont.minute.max()
    s["C_auction"] = auc.close.last()
    s["n_auction"] = auc.size()
    s = s.fillna({"n_cont_minutes": 0, "n_auction": 0})
    s["C"] = s.C_auction.where(s.has_auction, s.C_cont)
    s["missing_minutes"] = (s.t_minutes - s.n_cont_minutes).astype(int)
    s["duplicates"] = s.n_bars - s.n_distinct
    s["edge_ok"] = ((s.first_cont <= s.t_open + EDGE_MINUTES - 1)
                    & (s.last_cont >= s.t_close - EDGE_MINUTES + 1)).fillna(False)
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
    `status` is the price conversion outcome: the first failing rule of 6.2 and 6.3. Volume
    (6.4) and the final bar (Part B2) are separate field families: `volume_ok`,
    `final_bar_convention`; see `field_validity`."""
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
    d["final_bar_convention"] = final_bar_convention(d.C / k, d.ref_close, d.raw_close,
                                                     d.has_auction.astype(bool))
    d["open_ok"] = (d.O / k / d.raw_open - 1).abs() <= OPEN_TOL

    status = pd.Series(np.where(fb, CONVERTED_FALLBACK, CONVERTED), index=d.index, dtype=object)
    checks = [
        (d.raw_high.isna() | d.ref_close.isna(), "excluded:no_nse_row"),
        (method.isna(), UNRESOLVED),
        (~((k > 0) & (k <= K_MAX)), "excluded:k_bound"),
        (~d.contained, "excluded:range_not_contained"),
        (~d.fallback_reach_ok, "excluded:fallback_unconfirmed"),
    ]
    decided = pd.Series(False, index=d.index)
    for mask, label in checks:
        hit = mask.fillna(True).astype(bool) & ~decided
        status[hit] = label
        decided |= hit
    d["status"] = status
    return d


def final_bar_convention(close_raw_equiv: pd.Series, last: pd.Series, official: pd.Series,
                         auction: pd.Series) -> pd.Series:
    """Part B2: which NSE close the session's final bar carries.

    Before the closing auction: official_close, last_trade, both or none (within 0.1%). With the
    auction the print must equal the official close (one definition): official_close or none."""
    to_last = _agree(close_raw_equiv, last).fillna(False)
    to_official = _agree(close_raw_equiv, official).fillna(False)
    out = np.select([to_official & to_last, to_official, to_last],
                    ["both", "official_close", "last_trade"], "none")
    out = np.where(auction & to_official, "official_close", np.where(auction, "none", out))
    return pd.Series(out, index=close_raw_equiv.index, dtype=object)


def field_validity(ledger: pd.DataFrame, missing_threshold: int | None = None) -> pd.DataFrame:
    """Validity per field family (policy `field_families`) for every session of a ledger.

    price      the price conversion is publishable, the structure passes, the session is not
               PARTIAL (edges present, missing minutes within the threshold when given)
    volume     price valid and the volume rule holds
    final_bar  price valid and the final bar matches a documented NSE close"""
    price = (ledger.status.isin(PUBLISHABLE) & ledger.structure_ok.fillna(False).astype(bool)
             & ledger.edge_ok.fillna(False).astype(bool))
    if missing_threshold is not None:
        price &= ledger.missing_minutes <= missing_threshold
    return pd.DataFrame({
        "price_valid": price,
        "volume_valid": price & ledger.volume_ok.fillna(False).astype(bool),
        "final_bar_valid": price & ledger.final_bar_convention.ne("none"),
    }, index=ledger.index)


# ---------------------------------------------------------------- bar conversion

def convert_bars(bars: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    """Canonical bars for the sessions whose `price_valid` is true (or, without that column,
    whose status is publishable). Volume of a session with invalid volume is still converted;
    its flag travels in the quality table and the read path enforces it.

    price: angel / k * price_factor; volume: angel / c / volume_factor (no rounding)."""
    keep = ledger.price_valid if "price_valid" in ledger else ledger.status.isin(PUBLISHABLE)
    ok = ledger[keep.astype(bool)][
        ["symbol", "session", "k", "price_factor", "vol_c", "volume_factor"]]
    b = bars if "session" in bars else with_clock(bars)
    b = b.merge(ok, on=["symbol", "session"], how="inner", validate="many_to_one")
    scale = b.price_factor / b.k
    out = b[["symbol", "session", "timestamp"]].copy()
    for col in ("open", "high", "low", "close"):
        out[col] = b[col] * scale
    out["volume"] = b.volume / b.vol_c / b.volume_factor
    return out.sort_values(["symbol", "timestamp"], kind="stable").reset_index(drop=True)
