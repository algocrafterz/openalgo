"""Trade-list statistics.

Two conventions decide whether a comparison means anything:

  BASIS POINTS, NOT ONLY R.  R is not comparable across configurations that use
  different stop widths. Widening the stop shrinks both the gross R and the cost in R,
  so an R-only table silently rewards wide stops. `gross_bps` is expectancy in basis
  points of notional, which is directly comparable with the cost line.

  A t-STATISTIC ON EVERY ROW.  With a few hundred trades most differences are noise.
  |t| > 2 means the result is unlikely to be chance; anything less is a coin flip
  dressed up as a finding.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def summary(trades, label: str = "") -> dict:
    if not trades:
        return {"label": label, "n": 0, "win": np.nan, "gross_bps": np.nan, "net_R": np.nan,
                    "t": np.nan, "total_R": 0.0, "payoff": np.nan, "stop_pct": np.nan, "max_dd_R": np.nan}
    r = np.array([t.r_net for t in trades])
    g = np.array([t.r_gross for t in trades])
    rp = np.array([t.risk_pct for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    eq = np.cumsum(r)
    t_stat = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 and r.std() > 0 else np.nan
    return {
        "label": label,
        "n": len(r),
        "win": round(100 * float((r > 0).mean()), 1),
        "gross_bps": round(float((g * rp).mean() * 100), 2),
        "net_R": round(float(r.mean()), 3),
        "t": round(float(t_stat), 2),
        "total_R": round(float(r.sum()), 1),
        "payoff": round(float(wins.mean() / abs(losses.mean())), 2) if len(wins) and len(losses) else np.nan,
        "stop_pct": round(float(np.median(rp)), 3),
        "max_dd_R": round(float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0, 1),
    }


def by_symbol(trades) -> pd.DataFrame:
    """Dispersion across the basket. A result carried by two symbols is noise."""
    rows: dict[str, list[float]] = {}
    for t in trades:
        rows.setdefault(t.symbol, []).append(t.r_net)
    return pd.DataFrame([
        {"symbol": s.replace(".NS", ""), "n": len(v),
         "total_R": round(float(np.sum(v)), 1), "net_R": round(float(np.mean(v)), 3)}
        for s, v in rows.items()]).sort_values("total_R", ascending=False)


def by_reason(trades) -> pd.DataFrame:
    """Exit mix. Tells you whether the strategy is actually reaching its target."""
    df = pd.DataFrame([{"reason": t.reason, "r": t.r_net} for t in trades])
    if df.empty:
        return df
    g = df.groupby("reason").agg(n=("r", "size"), net_R=("r", "mean"), total_R=("r", "sum")).round(3)
    g["pct"] = (100 * g["n"] / len(df)).round(1)
    return g.sort_values("n", ascending=False)


def table(rows) -> str:
    return pd.DataFrame(rows).to_string(index=False)


def trades_frame(trades) -> pd.DataFrame:
    """Full trade log, for inspecting individual trades or exporting."""
    return pd.DataFrame([{
        "symbol": t.symbol, "day": t.day, "dir": "LONG" if t.direction == 1 else "SHORT",
        "tag": t.tag, "entry_time": t.entry_time, "entry": t.entry, "sl": t.sl,
        "tp": t.tp, "exit_time": t.exit_time, "exit": t.exit, "reason": t.reason,
        "risk_pct": round(t.risk_pct, 3), "r_gross": round(t.r_gross, 3),
        "r_net": round(t.r_net, 3)} for t in trades])
