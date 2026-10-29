"""Tables for the v3 historical replay; each source is reported on its own, never mixed.

    PYTHONPATH=. uv run python <this dir>/analyze.py

Reads results/<source>_*.csv from replay.py. The acceptance status uses the recorder's own
`metrics` and `evaluate` with the frozen reference. Signals with a FILL_GAP (1m bars missing in
the limit window or before 14:45) and, for the published source, signals whose adjusted signal
price and raw fill price disagree by more than two ticks are excluded from the results and
counted, never read as unfilled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "results"
DATASET_START = "2017-01-02"  # first session of nse-intraday-5m-20261021-002
OOS_CUT = "2023-11-08"
DATASET_END = "2026-10-19"  # last session of nse-intraday-5m-20261021-002
HOLDOUT = "2026-10-29"


def period(day: str) -> str:
    if day < DATASET_START:
        return "pre-dataset (Historify only, never examined)"
    if day < OOS_CUT:
        return "dev IS (v1/v2 in-sample, studies)"
    if day <= DATASET_END:
        return "dev OOS (seen by v1/v2, limit and IB studies)"
    if day < HOLDOUT:
        return "post-dataset, pre-freeze"
    return "hold-out"


def _t(x: pd.Series, day: pd.Series) -> float:
    d = x.groupby(day).mean()
    return (
        float(d.mean() / (d.std() / np.sqrt(len(d))))
        if len(d) > 2 and d.std() > 0
        else float("nan")
    )


def summary(s: pd.DataFrame) -> dict:
    filled = s["status"] == "FILLED"
    per = s["per_signal_bps"].astype(float)
    mkt = s["mkt_net_bps"].astype(float)
    return {
        "signals": len(s),
        "fills": int(filled.sum()),
        "fill_rate": filled.mean() if len(s) else np.nan,
        "cap_skip": int((s["status"] == "CAPACITY_SKIP").sum()),
        "net_sig": per.mean(),
        "t_sig": _t(per, s["day"]),
        "net_fill": s.loc[filled, "net_bps"].mean(),
        "stress_sig": s["net_bps_stress"].where(filled, 0.0).astype(float).mean(),
        "mkt_sig": mkt.mean(),
        "mkt_filled": mkt[filled].mean(),
        "mkt_missed": mkt[~filled].mean(),
    }


def fmt(d: dict) -> str:
    return (
        f"n {d['signals']:5d} fills {d['fills']:4d} ({d['fill_rate']:.1%}) cap {d['cap_skip']:2d} | "
        f"net/signal {d['net_sig']:+6.2f} (t {d['t_sig']:+.2f}) net/fill {d['net_fill']:+6.2f} "
        f"stress {d['stress_sig']:+6.2f} | market/signal {d['mkt_sig']:+6.2f} "
        f"[filled {d['mkt_filled']:+6.2f} missed {d['mkt_missed']:+6.2f}]"
    )


def report(source: str) -> pd.DataFrame | None:
    from signal_engine.analysis import idea034_paper as m

    p = OUT / f"{source}_signals.csv"
    if not p.exists():
        print(f"\n== {source}: no results")
        return None
    s = pd.read_csv(p)
    days = pd.read_csv(OUT / f"{source}_days.csv")
    sym = pd.read_csv(OUT / f"{source}_symbol_days.csv")
    s["year"] = s["day"].str[:4]
    print(f"\n================ SOURCE {source} ================")
    print(f"sessions {len(days)}; day status {days['status'].value_counts().to_dict()}")
    sym["year"] = sym["day"].str[:4]
    print("symbol-session states by year (NOT_LISTED is before a symbol's first bar):")
    print(
        sym.pivot_table(
            index="year", columns="state", values="symbol", aggfunc="count", fill_value=0
        ).to_string()
    )
    gap_days = days.loc[days["status"] == "DATA_GAP", "day"].str[:4].value_counts().sort_index()
    print(
        "whole-day DATA_GAP sessions (recorder rule: under 90% of symbols complete) by year:",
        gap_days.to_dict(),
    )
    excl = s["fill_gap"].astype(bool)
    print(f"signals {len(s)}; FILL_GAP excluded {int(excl.sum())}")
    if source.startswith("published"):
        bad = ~s["basis_ok"].astype(bool) & ~excl
        r = s["basis_ratio"].dropna()
        print(
            f"price basis (published 5m close / raw 1m close at the signal): median "
            f"{r.median():.5f}, |ratio-1| p95 {(r - 1).abs().quantile(0.95):.4%}, "
            f"beyond two ticks {int(bad.sum())} excluded"
        )
        excl = excl | bad
    s = s[~excl].copy()
    s["period"] = s["day"].map(period)
    print("\nOVERALL      " + fmt(summary(s)))
    for side, g in s.groupby("direction"):
        print(f"{'long' if side > 0 else 'short':12} " + fmt(summary(g)))
    print("\nby development period:")
    for per_, g in s.groupby("period"):
        print(f"  {per_:48} " + fmt(summary(g)))
    print("\nby year:")
    for y, g in s.groupby("year"):
        print(f"  {y} " + fmt(summary(g)))
    print(
        "\nexit reasons (filled):",
        s.loc[s["status"] == "FILLED", "exit_reason"].value_counts().to_dict(),
    )
    ok_days = days[days["status"] == "OK"]
    met = m.metrics(s.sort_values(["signal_time", "symbol"]), days)
    ref = json.loads(m.REFERENCE.read_text(encoding="utf-8"))
    ver = m.evaluate(met, ref)
    print(
        f"\nrecorder metrics: max drawdown {met['max_drawdown']:.0f} bps = Rs "
        f"{met['max_drawdown'] * 10:,.0f}; cumulative Rs {met['cum_net'] * 10:+,.0f} on "
        f"{len(ok_days)} OK sessions"
    )
    print(
        f"recorder acceptance status (frozen criteria, applied to HISTORY, not a hold-out "
        f"verdict): {ver['status']}: {'; '.join(ver['reasons'])}"
    )
    return s


def parity(h: pd.DataFrame | None, p: pd.DataFrame | None) -> None:
    if h is None or p is None:
        return
    key = ["day", "symbol", "direction"]
    lo, hi = max(h["day"].min(), p["day"].min()), min(h["day"].max(), p["day"].max())
    hh = h[(h["day"] >= lo) & (h["day"] <= hi)]
    pp = p[(p["day"] >= lo) & (p["day"] <= hi)]
    both = hh.merge(pp, on=key, suffixes=("_h", "_p"))
    print(f"\n================ S1 vs S2 signal parity ({lo} to {hi}, after exclusions) ======")
    for y in sorted(set(hh["year"]) | set(pp["year"])):
        nh, np_ = int((hh["year"] == y).sum()), int((pp["year"] == y).sum())
        nb = int((both["year_h"] == y).sum())
        print(
            f"  {y}: historify {nh:4d} published {np_:4d} common {nb:4d} "
            f"({nb / max(nh, np_, 1):.0%} of the larger)"
        )


def main(argv: list[str]) -> int:
    tags = argv or ["historify", "published", "historify_listed", "published_listed"]
    got = {t: report(t) for t in tags}
    for a, b in (("historify", "published"), ("historify_listed", "published_listed")):
        if a in got and b in got:
            print(f"\n[{a} vs {b}]")
            parity(got[a], got[b])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
