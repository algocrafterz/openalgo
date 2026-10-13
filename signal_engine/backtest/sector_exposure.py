"""Sector and correlation exposure of a run's trade list (post-run, report-only).

Pure functions over a trade table; no bars, no engine change, no trial created. Deterministic:
the result does not depend on row order and is sealed with a content hash.

  SHARES.        Per sector: trades, share of trades, notional-days and its share, net R and its
                 share of total net R. Notional is one unit per trade (the engine sizes a fixed
                 notional), so notional-days is the summed holding time in days. Top-sector share
                 and the Herfindahl index of the trade shares are reported.
  PEAK.          Per sector: the most positions open at once, and the time-weighted mean over the
                 time the sector held at least one position. A position that exits at the instant
                 another enters has already freed its slot.
  CORRELATION.   Per symbol pair: Pearson correlation of daily trade R (R summed per entry day) on
                 the days both symbols traded. A pair with fewer than `min_overlap` common days is
                 'unmeasured' with corr None, never zero. The average pairwise correlation is the
                 mean over measured pairs only, None when no pair is measured.
  DENOMINATOR.   Shares are over sector-mapped trades only, so they sum to one and the Herfindahl
                 index is well defined; unmapped trades are counted separately below.
  UNMAPPED.      Every symbol absent from the sector map is listed by name, and its trades are
                 counted separately; none is dropped silently.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from signal_engine.backtest.cost_contribution import seal
from signal_engine.timeutils import IST

SCHEMA = "sector_exposure/1"
MIN_OVERLAP_FLOOR = 2
MAX_PAIRS_LISTED = 500
COLUMNS = ("symbol", "entry_time", "exit_time", "r_net")
DAY_NS = 24 * 3600 * 10**9

LIMITATIONS = (
    "static sector map (sectors.yaml), not point-in-time: reclassifications and listings are ignored",
    "notional is one unit per trade, so notional-days is holding time, not rupees",
    "correlation uses daily trade R on days both symbols entered a trade, not return series; "
    "it is not a risk-model covariance and holding-interval overlap within the day is not checked",
    "trade R is selected by the strategy's own signals, so pair correlation is conditional on both firing",
    "a symbol listed in more than one sector is counted in the first listed sector only",
)


class SectorExposureError(ValueError):
    """The inputs cannot support the report."""


def load_sector_map(path: str | Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Symbol to sector. A symbol in several sectors goes to the first; all are returned."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or not raw:
        raise SectorExposureError(f"{path} holds no sector map")
    mapping: dict[str, str] = {}
    seen: dict[str, list[str]] = {}
    for sector, symbols in raw.items():
        for sym in symbols or []:
            seen.setdefault(str(sym), []).append(str(sector))
            mapping.setdefault(str(sym), str(sector))
    return mapping, {s: v for s, v in seen.items() if len(v) > 1}


def trades_to_frame(trades: Iterable[Any]) -> pd.DataFrame:
    """The columns this report needs, from engine Trade objects."""
    rows = [(t.symbol, t.entry_time, t.exit_time, t.r_net) for t in trades]
    return pd.DataFrame(rows, columns=list(COLUMNS))


def _prepare(trades: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in COLUMNS if c not in trades.columns]
    if missing:
        raise SectorExposureError(f"trade table lacks columns: {', '.join(missing)}")
    if trades.empty:
        raise SectorExposureError("trade table is empty")
    t = trades.loc[:, list(COLUMNS)].copy()
    if t["exit_time"].isna().any() or t["entry_time"].isna().any():
        raise SectorExposureError("every trade needs an entry and an exit time")
    for col in ("entry_time", "exit_time"):
        s = pd.to_datetime(t[col])
        s = s.dt.tz_localize(IST) if s.dt.tz is None else s.dt.tz_convert(IST)
        t[col] = s
    if (t["exit_time"] < t["entry_time"]).any():
        raise SectorExposureError("a trade exits before it enters")
    t["r_net"] = t["r_net"].astype(float)
    return t.sort_values(["entry_time", "symbol", "exit_time"], kind="stable").reset_index(
        drop=True
    )


def _peak(group: pd.DataFrame) -> dict[str, Any]:
    ev = [(e, 1) for e in group["entry_time"]] + [(x, -1) for x in group["exit_time"]]
    ev.sort(key=lambda p: (p[0], p[1]))  # exits (-1) before entries at one instant
    level = peak = 0
    prev = None
    busy_ns = weighted_ns = 0
    for when, delta in ev:
        if prev is not None and level > 0:
            span = when.value - prev.value
            busy_ns += span
            weighted_ns += span * level
        level += delta
        peak = max(peak, level)
        prev = when
    mean = (weighted_ns / busy_ns) if busy_ns else None
    return {
        "peak_simultaneous": int(peak),
        "mean_simultaneous_when_active": mean and round(mean, 4),
    }


def _shares(t: pd.DataFrame, sector_of: pd.Series) -> dict[str, Any]:
    t = t.assign(sector=sector_of)
    days = (t["exit_time"] - t["entry_time"]).astype("int64") / DAY_NS
    mapped = t[t["sector"].notna()]
    total_days = float(days.loc[mapped.index].sum())
    total_r = float(mapped["r_net"].sum())
    out: dict[str, Any] = {}
    for sector, g in mapped.groupby("sector", sort=True):
        nd = float(days.loc[g.index].sum())
        out[str(sector)] = {
            "trades": int(len(g)),
            "trade_share": len(g) / len(mapped),
            "notional_days": nd,
            "notional_days_share": nd / total_days if total_days else None,
            "net_r": float(g["r_net"].sum()),
            "net_r_share": float(g["r_net"].sum()) / total_r if total_r else None,
            "symbols": sorted(g["symbol"].unique().tolist()),
            **_peak(g),
        }
    return out


def _pair_stats(t: pd.DataFrame, min_overlap: int) -> dict[str, Any]:
    day = t["entry_time"].dt.strftime("%Y-%m-%d")
    daily = t.assign(day=day).groupby(["day", "symbol"])["r_net"].sum().unstack("symbol")
    syms = list(daily.columns)
    x = daily.to_numpy(dtype=float)
    m = ~np.isnan(x)
    x0 = np.where(m, x, 0.0)
    mf = m.astype(float)
    n = mf.T @ mf
    sx, sy = x0.T @ mf, mf.T @ x0
    sxy, sxx, syy = x0.T @ x0, (x0**2).T @ mf, mf.T @ (x0**2)
    with np.errstate(divide="ignore", invalid="ignore"):
        cov = sxy - sx * sy / n
        var = (sxx - sx**2 / n) * (syy - sy**2 / n)
        corr = cov / np.sqrt(var)
    pairs = []
    for i, j in itertools.combinations(range(len(syms)), 2):
        ov = int(n[i, j])
        ok = ov >= min_overlap and np.isfinite(corr[i, j])
        pairs.append(
            {
                "pair": f"{syms[i]}|{syms[j]}",
                "overlap_days": ov,
                "corr": round(float(corr[i, j]), 6) if ok else None,
                "status": "measured" if ok else "unmeasured",
            }
        )
    measured = [p for p in pairs if p["corr"] is not None]
    avg = round(float(np.mean([p["corr"] for p in measured])), 6) if measured else None
    listed = sorted(measured, key=lambda p: (-p["corr"], p["pair"]))
    listed += sorted((p for p in pairs if p["corr"] is None), key=lambda p: p["pair"])
    return {
        "basis": "daily trade R summed per entry day, common days of the two symbols",
        "min_overlap": min_overlap,
        "symbols": len(syms),
        "total_pairs": len(pairs),
        "measured_pairs": len(measured),
        "unmeasured_pairs": len(pairs) - len(measured),
        "average_pairwise": avg,
        "pairs_listed_cap": MAX_PAIRS_LISTED,
        "pairs": listed[:MAX_PAIRS_LISTED],
    }


def build_report(
    trades: pd.DataFrame, sector_map: dict[str, str], *, min_overlap: int = 20
) -> dict[str, Any]:
    if min_overlap < MIN_OVERLAP_FLOOR:
        raise SectorExposureError(f"min_overlap must be at least {MIN_OVERLAP_FLOOR}")
    t = _prepare(trades)
    sector_of = t["symbol"].map(sector_map)
    by_sector = _shares(t, sector_of)
    shares = [v["trade_share"] for v in by_sector.values()]
    top = max(by_sector, key=lambda s: (by_sector[s]["trade_share"], s)) if by_sector else None
    unmapped = sorted(t.loc[sector_of.isna(), "symbol"].unique().tolist())
    return {
        "trades": int(len(t)),
        "by_sector": by_sector,
        "top_sector": {"sector": top, "trade_share": by_sector[top]["trade_share"]}
        if top
        else None,
        "herfindahl": float(sum(s * s for s in shares)) if shares else None,
        "unmapped_symbols": unmapped,
        "unmapped_trades": int(sector_of.isna().sum()),
        "correlation": _pair_stats(t, min_overlap),
        "limitations": list(LIMITATIONS),
    }


def build_evidence(
    trades: pd.DataFrame,
    sector_map: dict[str, str],
    *,
    min_overlap: int = 20,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    t = _prepare(trades)
    inputs = hashlib.sha256(
        t.assign(entry_time=t.entry_time.astype(str), exit_time=t.exit_time.astype(str))
        .to_json(orient="records")
        .encode()
    ).hexdigest()
    body = {
        "schema": SCHEMA,
        "report_only": True,
        "diagnostic_not_trials": True,
        "source": {
            **(source or {}),
            "trades_sha256": inputs,
            "sector_map_sha256": hashlib.sha256(
                json.dumps(sector_map, sort_keys=True).encode()
            ).hexdigest(),
        },
        "report": build_report(trades, sector_map, min_overlap=min_overlap),
    }
    return {**body, "content_sha256": seal(body)}


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if evidence.get("schema") != SCHEMA:
        problems.append(f"schema is {evidence.get('schema')!r}, expected {SCHEMA!r}")
    body = {k: v for k, v in evidence.items() if k != "content_sha256"}
    if evidence.get("content_sha256") != seal(body):
        problems.append("content_sha256 does not match the payload")
    if evidence.get("report_only") is not True or evidence.get("diagnostic_not_trials") is not True:
        problems.append("report_only and diagnostic_not_trials must both be true")
    return problems


__all__: Sequence[str] = (
    "build_report",
    "build_evidence",
    "validate_evidence",
    "load_sector_map",
    "trades_to_frame",
)
