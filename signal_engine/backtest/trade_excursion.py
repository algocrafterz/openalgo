"""MAE/MFE per recorded trade: a read-only diagnostic on a run's trades and the published bars.

Design (proposed 2026-10-09, for review before any recording integration):

  SOURCE.      A run's `evidence/trades.parquet` and `evidence/signals.parquet`, and the 5-minute
               bars of the dataset version the run recorded. Nothing is simulated, no strategy
               runs, no trial is created, the run record is never written to.
  DIRECTION.   d = +1 for BUY, -1 for SELL. From the fill price E:
                 favourable excursion = d * (extreme - E) on the favourable side, floored at 0
                 adverse excursion    = d * (E - extreme) on the adverse side, floored at 0
               MFE is the largest favourable excursion and MAE the largest adverse one, over the
               holding period. Both are stored as positive numbers.
  UNITS.       Price, bps of E, and R. One R is the signal's own `risk` in price (the unit the
               engine's gross R uses). Zero, negative or missing risk makes the R figures
               undefined; the price and bps figures remain.
  PATH.        Bars from the entry bar (the fill is its open) to the exit bar, both inclusive. A
               gap in the 5-minute sequence, a missing bar or a missing symbol leaves the trade
               out of the distributions and is counted by status.
  EXIT BAR.    Inside one bar the order of the high and the low is unknown. Two readings bracket
               the answer and both are stored:
                 known_path  the exit bar counts only the open and the exit price (a stop exit
                             credits no favourable move after the open, a target exit no adverse
                             move after the open). A lower bound on both excursions.
                 full_bar    the exit bar counts its full range, clipped at the exit price on the
                             side the exit happened. An upper bound.
               A time or end-of-day exit fills at the bar close, so the whole bar was held and
               both readings agree.
  LIMITS.      Excursions say where a trade went, not what a different exit would have earned:
               a path measured under one exit rule cannot be replayed under another, and a
               better-looking exit would need its own hypothesis and out-of-sample test.
  GATING.      None. Report only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.cost_contribution import seal
from signal_engine.timeutils import IST

SCHEMA = "trade_excursion/1"
BAR_MINUTES = 5
PERCENTILES = (5, 10, 25, 50, 75, 90, 95, 99)
MIN_GROUP = 30
MAE_EDGES = (0.0, 0.25, 0.5, 0.75, 1.0)
MFE_EDGES = (0.0, 0.25, 0.5, 1.0, 1.5)
CLOSE_EXITS = ("TIME_EXIT", "EOD", "DATA_GAP")
STOP_EXITS = ("SL", "SL_GAP")
REQUIRED = (
    "symbol",
    "side",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "exit_reason",
    "gross_r",
    "net_r",
    "risk",
)
OK = "ok"


class ExcursionError(ValueError):
    """The inputs cannot support the diagnostic."""


def _num(x: Any) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def _ns(ts: pd.Series) -> np.ndarray:
    return pd.to_datetime(ts, utc=True).dt.tz_convert("UTC").astype("int64").to_numpy()


def _bar_arrays(bars: pd.DataFrame) -> tuple[np.ndarray, ...]:
    idx = pd.DatetimeIndex(bars.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return (
        idx.astype("int64").to_numpy(),
        bars["Open"].to_numpy(float),
        bars["High"].to_numpy(float),
        bars["Low"].to_numpy(float),
        bars["Close"].to_numpy(float),
    )


def _exit_bar_excursions(
    d: int, entry: float, exit_price: float, reason: str, o: float, h: float, low: float, rule: str
) -> tuple[float, float]:
    """(favourable, adverse) excursion the exit bar contributes under `rule`."""
    fav_full = max(0.0, d * ((h if d == 1 else low) - entry))
    adv_full = max(0.0, d * (entry - (low if d == 1 else h)))
    fav_exit = max(0.0, d * (exit_price - entry))
    adv_exit = max(0.0, d * (entry - exit_price))
    fav_open = max(0.0, d * (o - entry))
    adv_open = max(0.0, d * (entry - o))
    if reason in CLOSE_EXITS:
        return fav_full, adv_full
    stop_side = reason in STOP_EXITS
    if rule == "full_bar":
        return (fav_full, adv_exit) if stop_side else (fav_exit, adv_full)
    return (fav_open, adv_exit) if stop_side else (fav_exit, adv_open)


def measure_trade(
    d: int, entry: float, exit_price: float, reason: str, o, h, low, rule: str
) -> tuple[float, float]:
    """(MFE, MAE) in price for one trade from its bar slices (entry bar first, exit bar last)."""
    n = len(o)
    fav = adv = 0.0
    if n > 1:
        hi = h[: n - 1].max()
        lo = low[: n - 1].min()
        fav = max(0.0, d * ((hi if d == 1 else lo) - entry))
        adv = max(0.0, d * (entry - (lo if d == 1 else hi)))
    f2, a2 = _exit_bar_excursions(d, entry, exit_price, reason, o[-1], h[-1], low[-1], rule)
    return max(fav, f2), max(adv, a2)


def measure(trades: pd.DataFrame, bars: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per trade with its status and the excursions under both exit-bar readings."""
    missing = [c for c in REQUIRED if c not in trades.columns]
    if missing:
        raise ExcursionError(f"trade list lacks columns: {', '.join(missing)}")
    if trades.empty:
        raise ExcursionError("trade list is empty")
    t = trades.reset_index(drop=True)
    sym_a, side_a = t["symbol"].to_numpy(), t["side"].to_numpy()
    entry_a, exit_a = t["entry_price"].to_numpy(float), t["exit_price"].to_numpy(float)
    reason_a = t["exit_reason"].to_numpy()
    en, ex = _ns(t["entry_time"]), _ns(t["exit_time"])
    step = BAR_MINUTES * 60 * 10**9
    arrays: dict[str, tuple] = {}
    n = len(t)
    status = np.full(n, OK, dtype=object)
    names = (
        "mfe_known", "mae_known", "mfe_full", "mae_full", "n_bars",
        "entry_open_gap", "exit_in_range", "exit_at_close",
    )
    out = {k: np.full(n, np.nan) for k in names}
    for i in range(n):
        sym = sym_a[i]
        if sym not in bars:
            status[i] = "no_bars"
            continue
        if sym not in arrays:
            arrays[sym] = _bar_arrays(bars[sym])
        ts, o, h, low, c = arrays[sym]
        a = int(np.searchsorted(ts, en[i]))
        b = int(np.searchsorted(ts, ex[i]))
        if a >= len(ts) or ts[a] != en[i]:
            status[i] = "entry_bar_missing"
            continue
        if b >= len(ts) or ts[b] != ex[i] or b < a:
            status[i] = "exit_bar_missing"
            continue
        if b - a + 1 != (ex[i] - en[i]) // step + 1:
            status[i] = "path_gap"
            continue
        d = 1 if side_a[i] == "BUY" else -1
        entry, xp, reason = float(entry_a[i]), float(exit_a[i]), reason_a[i]
        sl = slice(a, b + 1)
        out["n_bars"][i] = b - a + 1
        out["entry_open_gap"][i] = abs(o[a] - entry)
        out["exit_in_range"][i] = float(low[b] - 1e-9 <= xp <= h[b] + 1e-9)
        out["exit_at_close"][i] = float(abs(c[b] - xp) < 1e-9)
        for rule, kf, ka in (
            ("known_path", "mfe_known", "mae_known"),
            ("full_bar", "mfe_full", "mae_full"),
        ):
            out[kf][i], out[ka][i] = measure_trade(d, entry, xp, reason, o[sl], h[sl], low[sl], rule)
    res = t[["symbol", "side", "exit_reason", "gross_r", "net_r"]].copy()
    if "trade_id" in t:
        res.insert(0, "trade_id", t["trade_id"])
    res["year"] = pd.to_datetime(t["entry_time"], utc=True).dt.tz_convert(IST).dt.year
    res["risk"] = t["risk"].astype(float)
    res["entry_price"] = entry_a
    res["status"] = status
    for k, v in out.items():
        res[k] = v
    for extra in ("target_r", "stop_r"):
        if extra in t:
            res[extra] = t[extra].astype(float)
    ok_risk = np.isfinite(res["risk"]) & (res["risk"] > 0)
    res["risk_defined"] = ok_risk
    safe_risk = res["risk"].where(ok_risk)
    for rule in ("known", "full"):
        for kind in ("mfe", "mae"):
            p = res[f"{kind}_{rule}"]
            res[f"{kind}_{rule}_r"] = p / safe_risk
            res[f"{kind}_{rule}_bps"] = p / res["entry_price"] * 1e4
    res["giveback_r"] = res["mfe_known_r"] - res["gross_r"]
    return res


def dist(s: pd.Series) -> dict[str, Any]:
    """Count, mean and percentiles of a series; values are withheld below MIN_GROUP."""
    s = s.dropna()
    if len(s) < MIN_GROUP:
        return {"n": int(len(s)), "withheld": f"fewer than {MIN_GROUP} observations"}
    q = np.percentile(s, PERCENTILES)
    return {"n": int(len(s)), "mean": _num(s.mean()), **{f"p{p}": _num(v) for p, v in zip(PERCENTILES, q)}}


def _excursion_block(g: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {"trades": int(len(g))}
    for rule in ("known", "full"):
        for kind in ("mae", "mfe"):
            out[f"{kind}_{rule}_r"] = dist(g[f"{kind}_{rule}_r"])
            out[f"{kind}_{rule}_bps"] = dist(g[f"{kind}_{rule}_bps"])
    out["gross_r"] = dist(g["gross_r"])
    out["giveback_r_known"] = dist(g["giveback_r"])
    return out


def _bucket(g: pd.DataFrame, col: str, edges: Sequence[float]) -> list[dict[str, Any]]:
    bins = [*edges, np.inf]
    rows = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = g[(g[col] >= lo) & (g[col] < hi)]
        rows.append(
            {
                "from": lo,
                "to": None if hi == np.inf else hi,
                "trades": int(len(m)),
                "share": _num(len(m) / len(g)) if len(g) else None,
                "win_rate_net": _num((m["net_r"] > 0).mean()) if len(m) else None,
                "gross_r_per_trade": _num(m["gross_r"].mean()) if len(m) else None,
                "net_r_per_trade": _num(m["net_r"].mean()) if len(m) else None,
            }
        )
    return rows


def _consistency(g: pd.DataFrame) -> dict[str, Any]:
    """Cross-checks that the measured path agrees with the exit rules the engine applied."""
    out: dict[str, Any] = {}
    if "target_r" in g:
        sl = g[g["exit_reason"].isin([*STOP_EXITS, *CLOSE_EXITS]) & g["target_r"].notna()]
        hit = sl["mfe_known_r"] >= sl["target_r"] - 1e-6
        out["stopped_or_timed_trades_whose_known_path_reached_the_target"] = int(hit.sum())
        out["stopped_or_timed_trades_checked"] = int(len(sl))
    if "stop_r" in g:
        tp = g[(g["exit_reason"] == "TP") & g["stop_r"].notna()]
        hit = tp["mae_known_r"] >= tp["stop_r"] - 1e-6
        out["target_trades_whose_known_path_reached_the_stop"] = int(hit.sum())
        out["target_trades_checked"] = int(len(tp))
    return out


def summarise(res: pd.DataFrame) -> dict[str, Any]:
    """The report body: status counts, reconciliation, distributions, buckets, consistency."""
    ok = res[res["status"] == OK]
    usable = ok[ok["risk_defined"]]
    out: dict[str, Any] = {
        "trades_total": int(len(res)),
        "status_counts": {str(k): int(v) for k, v in res["status"].value_counts().items()},
        "trades_with_complete_path": int(len(ok)),
        "trades_with_defined_r": int(len(usable)),
        "trades_with_undefined_risk": int((~ok["risk_defined"]).sum()),
    }
    if usable.empty:
        return out
    out["reconciliation"] = {
        "entry_price_equals_entry_bar_open": _num((ok["entry_open_gap"] < 1e-6).mean()),
        "exit_price_within_exit_bar_range": _num(ok["exit_in_range"].mean()),
        "close_exits_at_bar_close": _num(
            ok.loc[ok["exit_reason"].isin(CLOSE_EXITS), "exit_at_close"].mean()
        ),
        "trades_where_exit_bar_reading_changes_mae_r": int(
            (usable["mae_full_r"] - usable["mae_known_r"] > 1e-9).sum()
        ),
        "trades_where_exit_bar_reading_changes_mfe_r": int(
            (usable["mfe_full_r"] - usable["mfe_known_r"] > 1e-9).sum()
        ),
    }
    out["all"] = _excursion_block(usable)
    out["by_side"] = {str(k): _excursion_block(g) for k, g in usable.groupby("side")}
    out["by_year"] = {str(k): _excursion_block(g) for k, g in usable.groupby("year")}
    out["by_exit_reason"] = {str(k): _excursion_block(g) for k, g in usable.groupby("exit_reason")}
    out["by_net_outcome"] = {
        "net_winners": _excursion_block(usable[usable["net_r"] > 0]),
        "net_losers": _excursion_block(usable[usable["net_r"] <= 0]),
    }
    out["mae_buckets_known_r"] = _bucket(usable, "mae_known_r", MAE_EDGES)
    out["mfe_buckets_known_r"] = _bucket(usable, "mfe_known_r", MFE_EDGES)
    pos = usable[usable["mfe_known_r"] > 0]
    capture = (pos["gross_r"] / pos["mfe_known_r"]).replace([np.inf, -np.inf], np.nan)
    out["capture_ratio_gross_r_over_mfe"] = {
        "definition": "gross R / MFE R (known path), trades with MFE > 0; can be negative",
        **dist(capture),
    }
    out["spearman_mfe_vs_gross_r"] = _num(
        usable["mfe_known_r"].corr(usable["gross_r"], method="spearman")
    )
    out["spearman_mae_vs_gross_r"] = _num(
        usable["mae_known_r"].corr(usable["gross_r"], method="spearman")
    )
    out["consistency"] = _consistency(usable)
    return out


def build_evidence(res: pd.DataFrame, source: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema": SCHEMA,
        "report_only": True,
        "diagnostic_not_trials": True,
        "source": source,
        "summary": summarise(res),
    }
    return {**body, "content_sha256": seal(body)}


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    problems = []
    if evidence.get("schema") != SCHEMA:
        problems.append(f"schema is {evidence.get('schema')!r}, expected {SCHEMA!r}")
    body = {k: v for k, v in evidence.items() if k != "content_sha256"}
    if evidence.get("content_sha256") != seal(body):
        problems.append("content_sha256 does not match the payload")
    if evidence.get("report_only") is not True or evidence.get("diagnostic_not_trials") is not True:
        problems.append("report_only and diagnostic_not_trials must both be true")
    return problems


def trades_with_risk(run_dir: str | Path) -> pd.DataFrame:
    """Recorded trades joined to their signal's risk, stop distance and target distance in R."""
    ev = Path(run_dir) / "evidence"
    t = pd.read_parquet(ev / "trades.parquet")
    s = pd.read_parquet(ev / "signals.parquet")[["signal_id", "stop", "target", "risk"]]
    m = t.merge(s, on="signal_id", how="left", validate="many_to_one")
    d = np.where(m["side"] == "BUY", 1.0, -1.0)
    ok = m["risk"].notna() & (m["risk"] > 0)
    safe = m["risk"].where(ok)
    m["stop_r"] = d * (m["entry_price"] - m["stop"]) / safe
    m["target_r"] = d * (m["target"] - m["entry_price"]) / safe
    return m


def evidence_for_run(
    run_dir: str | Path, bars: Mapping[str, pd.DataFrame]
) -> tuple[dict[str, Any], pd.DataFrame]:
    run = Path(run_dir)
    res = measure(trades_with_risk(run), bars)
    manifest = run / "manifest.json"
    ev = run / "evidence"
    source = {
        "run_dir": run.name,
        "trades_parquet_sha256": hashlib.sha256((ev / "trades.parquet").read_bytes()).hexdigest(),
        "signals_parquet_sha256": hashlib.sha256((ev / "signals.parquet").read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest.exists()
        else None,
        "bar_minutes": BAR_MINUTES,
    }
    return build_evidence(res, source), res


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import pickle
    import sys

    p = argparse.ArgumentParser(description="MAE/MFE per trade for a recorded run (read only)")
    p.add_argument("run_dir")
    p.add_argument("--bars", required=True, help="pickle of {symbol: DataFrame} 5-minute bars")
    p.add_argument("--out", required=True, help="directory for excursion.json and trades parquet")
    args = p.parse_args(argv)
    with open(args.bars, "rb") as fh:
        bars = pickle.load(fh)  # noqa: S301 - a file this workflow wrote
    evidence, res = evidence_for_run(args.run_dir, bars)
    problems = validate_evidence(evidence)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "excursion.json").write_text(json.dumps(evidence, indent=1, allow_nan=False))
    res.to_parquet(out / "excursion_trades.parquet")
    sys.stdout.write(f"wrote {out}; problems: {problems or 'none'}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
