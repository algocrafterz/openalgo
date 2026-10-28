"""Narrow vs wide IB% over 2017-2026, using signal_engine.features.ib_width (read-only).

    PYTHONPATH=. uv run python <this dir>/long_history.py <dir holding frames.pkl>

frames.pkl: the published 5m dataset nse-intraday-5m-20261021-002, built by
../2026-10-ib-reference-levels/inputs_frames.py. Trades: the IDEA-034 v1 trades and the P1
non-balance placebo, already costed with the primary model, from
../2026-10-ib-reference-levels/results/sample_a_*.csv (run sample_a.py first).
Terciles are cut on in-sample trades only (before 2023-11-08). t is clustered by day.
No BREAKINGTRADE long-history proxy exists: its picks come from the website's scans, which
start in August 2026.
"""

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.features.ib_width import ib_pct_at, ib_width

HERE = Path(__file__).resolve().parent
REF = HERE.parent / "2026-10-ib-reference-levels" / "results"
SETS = ("V1_N5_k1.5_p30_edge", "P1_non_balance")
CUT = pd.Timestamp("2023-11-08")
BAR_MIN = 5


def final_ib_pct(frame):
    """IB% per session as it stood at 10:15 (the 10:10 bar's value)."""
    w = ib_width(frame)
    at_close = w[(frame.index.hour == 10) & (frame.index.minute == 10)]
    return pd.Series(at_close["ib_pct"].to_numpy(), index=at_close.index.date)


def t_day(x, day):
    daily = x.groupby(day).mean()
    return daily.mean() / (daily.std() / np.sqrt(len(daily))) if len(daily) > 2 else np.nan


def table(df, col, masks):
    rows = []
    for b in ("narrow", "mid", "wide"):
        rec = {"bucket": b}
        for name, m in masks.items():
            s = df[m & (df[col] == b)]
            rec |= {
                f"{name}_n": len(s),
                f"{name}_gross": s.g.mean(),
                f"{name}_net": s.n.mean(),
                f"{name}_t": t_day(s.n, s.day),
            }
        rows.append(rec)
    return pd.DataFrame(rows).round(2)


def bucket(x, lo, hi):
    return np.select([x < lo, x > hi], ["narrow", "wide"], "mid")


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    frames = pickle.load(open(Path(sys.argv[1]) / "frames.pkl", "rb"))["frames"]
    trades = pd.concat([pd.read_csv(REF / f"sample_a_{s}.csv") for s in SETS], ignore_index=True)
    trades = trades[["set", "sym", "day", "et", "d", "g", "n"]]
    ibp = {s: final_ib_pct(frames[s]) for s in trades.sym.unique()}
    trades["ibp"] = [
        ibp[s].get(pd.Timestamp(d).date(), np.nan)
        for s, d in zip(trades.sym, trades.day, strict=True)
    ]
    print(f"IB% found for {trades.ibp.notna().sum()} of {len(trades)} trades")

    sample = trades.dropna(subset=["ibp"]).sample(80, random_state=11)
    bad = 0
    for r in sample.itertuples():
        et = pd.Timestamp(r.et)
        f = frames[r.sym]
        v = ib_pct_at(f[f.index < et], et, BAR_MIN)
        bad += not (v is not None and np.isclose(v, r.ibp))
    print("look-ahead check: mismatches in 80 trades recomputed from bars before entry:", bad)

    trades = trades.dropna(subset=["ibp"])
    for s in SETS:
        df = trades[trades.set == s].copy()
        is_ = pd.to_datetime(df.day) < CUT
        lo, hi = df.loc[is_, "ibp"].quantile([1 / 3, 2 / 3])
        df["terc"] = bucket(df.ibp, lo, hi)
        df["site"] = bucket(df.ibp, 30, 90)
        print(f"\n######## {s}: {len(df)} trades; IS tercile edges IB% {lo:.1f} / {hi:.1f}")
        per = {"IS": is_, "OOS": ~is_}
        side = {"L": df.d == 1, "S": df.d == -1}
        print("-- IB% terciles (pre-registered), by period")
        print(table(df, "terc", per).to_string(index=False))
        print("-- IB% terciles, by side (all years)")
        print(table(df, "terc", side).to_string(index=False))
        print("-- site buckets <30 / 30-90 / >90 (descriptive), by period")
        print(table(df, "site", per).to_string(index=False))
        df.to_csv(HERE / "results" / f"long_history_{s}.csv", index=False)
