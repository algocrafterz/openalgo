"""Summaries for sample A (results/sample_a.csv) and sample B (results/sample_b.csv).

uv run python signal_engine/research/ideas/idea-034-multi-day-balance-breakout/limit-order-study/analyze.py a|b
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "results"


def tstat(x, days):
    daily = pd.Series(x.to_numpy()).groupby(days.to_numpy()).mean()
    if len(daily) < 3 or daily.std() == 0:
        return np.nan
    return daily.mean() / (daily.std() / np.sqrt(len(daily)))


def per_signal(d, v, col="net"):
    return d[f"{v}|{col}"].where(d[f"{v}|filled"].astype(bool), 0.0)


def table(d, variants, periods, base="MKT"):
    rows = []
    for v in variants:
        filled = d[f"{v}|filled"].astype(bool)
        rec = {"variant": v, "fill%": round(100 * filled.mean(), 1)}
        sig = per_signal(d, v)
        rec["gross/fill"] = round(d.loc[filled, f"{v}|gross"].mean(), 2)
        rec["net/fill"] = round(d.loc[filled, f"{v}|net"].mean(), 2)
        rec["net/sig"] = round(sig.mean(), 2)
        rec["t/sig"] = round(tstat(sig, d.day), 2)
        rec["net8/sig"] = round(per_signal(d, v, "net8").mean(), 2)
        for name, m in periods:
            rec[f"{name}/sig"] = round(sig[m].mean(), 2)
        for side, m in (("L", d.dir > 0), ("S", d.dir < 0)):
            rec[f"{side}/sig"] = round(sig[m].mean(), 2)
        if v != base:
            b = d[f"{base}|net"]
            rec["mkt net: filled"] = round(b[filled].mean(), 2)
            rec["mkt net: missed"] = round(b[~filled].mean(), 2) if (~filled).any() else np.nan
        rows.append(rec)
    return pd.DataFrame(rows)


def main(which):
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    d = pd.read_csv(OUT / f"sample_{which}.csv")
    variants = [c.split("|")[0] for c in d.columns if c.endswith("|filled")]
    if which == "a":
        print("calibration (1m market replay vs recorded 5m engine):")
        print(
            f"  gross {d['MKT|gross'].mean():+.2f} vs {d.rec_gross.mean():+.2f}; "
            f"net {d['MKT|net'].mean():+.2f} vs {d.rec_net.mean():+.2f}; "
            f"reason agree {(d['MKT|reason'].replace('TP', 'TARGET') == d.rec_reason.replace({'TIME_EXIT': 'TIME', 'TARGET': 'TARGET'})).mean():.0%}; "
            f"per-trade |diff| median {np.median(np.abs(d['MKT|gross'] - d.rec_gross)):.1f} bps"
        )
        days = pd.to_datetime(d.day)
        periods = [("IS", days < "2023-11-08"), ("OOS", days >= "2023-11-08")]
        for label, sub in (("v1 all", d), ("v2 primary H%>=1.5", d[d.h_pct >= 1.5])):
            sp = [(n, m[sub.index]) for n, m in periods]
            print(f"\n== {label}: {len(sub)} signals")
            print(table(sub, variants, sp).to_string(index=False))
    else:
        periods = [("P1", d.day < "2026-09-24"), ("P2", d.day >= "2026-09-24")]
        print(
            f"calibration: market gross/share replay vs amt_B csv max |diff| "
            f"{d.calib_diff.abs().max():.4f}"
        )
        print(f"\n== BREAKINGTRADE baseline: {len(d)} signals")
        print(table(d, variants, periods).to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1])
