"""ORB regime-gate study (IDEA-035): pre-registered features, IS selection, OOS judgement.

Inputs:
  results/market_day_features.csv   from build_market_features.py
  <trades.parquet>                  ten-year legacy-settings ORB trades with net2/net5 columns
                                    (exploratory run of backtest/strategies/orb.py, dataset
                                    nse-intraday-5m-20261021-002; see idea.md)
  results/paper_replay_inputs.json  the 67 ORB paper trades with recorded sandbox R

Run:  PYTHONPATH=. uv run python signal_engine/research/ideas/idea-035-orb-regime-gate/regime_study.py <trades.parquet>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.backtest import costs as C
from signal_engine.backtest.metrics import clustered_t

HERE = Path(__file__).resolve().parent
RES = HERE / "results"
CUT = pd.Timestamp("2023-11-08")
T_BAR = 2.7  # Bonferroni, 7 features at 0.05
RISK_RS = 900.0
RNG = np.random.default_rng(7)


def _fmt(x: float) -> str:
    return f"{x:+.3f}"


def day_features(trades_days: pd.Index) -> pd.DataFrame:
    f = pd.read_csv(RES / "market_day_features.csv", parse_dates=["day"]).set_index("day")
    out = pd.DataFrame(index=f.index)
    out["F1"] = np.where(f.prev_ret_med > 0, "up", "down")
    out["F2"] = np.where(f.gap_med > 0, "up", "down")
    out["F3"] = np.where(f.drive_med > 0, "up", "down")
    out["F4"] = np.where(f.prev_breadth > 0.5, "above", "below")
    out["F5"] = np.where(f.rng_exp_med > 1.0, "above", "below")
    out["F6"] = np.where(f.above_sma20_breadth > 0.5, "above", "below")
    mkt = f.prev_ret_med  # prior-day median return; vol regime from its own history
    vol = mkt.abs().rolling(20).mean()
    out["F7"] = np.where(vol > vol.rolling(250, min_periods=60).median(), "high", "low")
    out.loc[f.rng_exp_med.isna() | f.prev_ret_med.isna(), :] = np.nan
    return out


def cell(g: pd.DataFrame, col: str = "net2") -> dict:
    if len(g) < 30:
        return {"n": len(g), "mean": np.nan, "t": np.nan}
    return {
        "n": len(g),
        "mean": g[col].mean(),
        "t": clustered_t(g[col].to_numpy(), g.day.to_numpy()),
    }


def gate_table(t: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    rows, chosen = [], {}
    for fid in ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]:
        for side in ("LONG", "SHORT"):
            s = t[(t.side == side) & t[fid].notna()]
            is_ = s[s.window == "IS"]
            states = sorted(s[fid].unique())
            is_means = {k: is_[is_[fid] == k].net2.mean() for k in states}
            best = max(is_means, key=lambda k: is_means[k])
            oos = s[(s.window == "OOS") & (s[fid] == best)]
            allst = s[s[fid] == best]
            yrs = oos.groupby("year").net2.mean()
            co, ca = cell(oos), cell(allst)
            gross_oos = oos.r_gross.mean()
            passed = (
                co["mean"] > 0
                and (yrs > 0).mean() > 0.5
                and co["t"] >= T_BAR
            )
            rows.append({
                "feature": fid, "side": side, "state_chosen_on_IS": best,
                "IS_net2": is_means[best], "OOS_n": co["n"], "OOS_gross": gross_oos,
                "OOS_net2": co["mean"], "OOS_t": co["t"],
                "OOS_years_pos": f"{int((yrs > 0).sum())}/{len(yrs)}",
                "ALL_net2": ca["mean"], "PASS": bool(passed),
            })
            chosen[(fid, side)] = best
    return pd.DataFrame(rows), chosen


def day_block_boot(df: pd.DataFrame, col: str, n: int = 4000) -> tuple[float, float]:
    """95 percent CI of sum(col)/N_baseline difference, resampling whole days."""
    g = df.groupby("day")[col].sum()
    cnt = df.groupby("day").size()
    idx = np.arange(len(g))
    vals = []
    for _ in range(n):
        s = RNG.choice(idx, size=len(idx))
        vals.append(g.values[s].sum() / cnt.values[s].sum())
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def variants_10y(t: pd.DataFrame, chosen: dict, tbl: pd.DataFrame) -> pd.DataFrame:
    rows = []
    passing = tbl[tbl.PASS]

    def add(name: str, w: pd.Series) -> None:
        for col in ("net2", "net5"):
            d = t.assign(w=w, x=t[col] * w, base=t[col])
            taken = d[d.w > 0]
            per_slot = d.x.sum() / len(d)
            diff = d.x - d.base
            lo, hi = day_block_boot(d.assign(diff=diff), "diff")
            for win, sub in (("ALL", d), ("OOS", d[d.window == "OOS"])):
                tk = sub[sub.w > 0]
                rows.append({
                    "variant": name, "cost": col, "window": win, "taken": len(tk),
                    "share_of_baseline": len(tk) / len(sub),
                    "mean_net_R_per_taken_trade": (tk.x / tk.w).mean(),
                    "net_R_per_baseline_slot": sub.x.sum() / len(sub),
                    "t_clustered": clustered_t(tk.x.to_numpy(), tk.day.to_numpy()) if len(tk) > 30 else np.nan,
                    "diff_vs_baseline_per_slot_CI95": f"[{lo:+.4f},{hi:+.4f}]" if win == "ALL" else "",
                })

    one = pd.Series(1.0, index=t.index)
    add("B0 baseline", one)
    add("B1 shorts only", (t.side == "SHORT").astype(float))
    for _, r in passing.iterrows():
        good = (t.side != r.side) | (t[r.feature] == r.state_chosen_on_IS)
        add(f"B2 gate {r.feature} {r.side}->{r.state_chosen_on_IS}", good.astype(float))
    # B3 is run for every feature/side that has a positive IS cell, as a size rule; report the
    # single strongest-on-IS pair only so the trial count stays honest.
    best = tbl.sort_values("IS_net2", ascending=False).iloc[0]
    good = (t.side != best.side) | (t[best.feature] == best.state_chosen_on_IS)
    add(f"B3 half size outside {best.feature} {best.side}->{best.state_chosen_on_IS}", good.map({True: 1.0, False: 0.5}))
    return pd.DataFrame(rows)


def paper_replay(chosen_feature: pd.DataFrame, tbl: pd.DataFrame) -> pd.DataFrame:
    sched = C._schedule("MIS", "NSE", "flattrade")
    o = pd.DataFrame(json.load(open(RES / "paper_replay_inputs.json")))
    o["day"] = pd.to_datetime(o.day)
    o["side"] = np.where(o.d == 1, "LONG", "SHORT")
    risk = (o.fill - o.sl).abs()
    notional = o.fill * o.qty
    stat = np.array([float(sched.charge(buy_value=n, sell_value=n, orders=2)) for n in notional]) / o.qty
    out = []
    feats = chosen_feature
    o = o.join(feats, on="day")
    for slip in (2, 5, 10):
        o[f"net{slip}"] = o.actual - (stat + o.fill * slip / 1e4 * 2) / risk
    have = o[o.F1.notna()]
    rows = []

    def boot(diff: np.ndarray) -> str:
        m = [RNG.choice(diff, size=len(diff)).mean() for _ in range(10000)]
        return f"[{np.percentile(m, 2.5):+.3f},{np.percentile(m, 97.5):+.3f}]"

    for slip in (2, 5, 10):
        base = have[f"net{slip}"].to_numpy()
        specs = {"B0 baseline": np.ones(len(have)), "B1 shorts only": (have.side == "SHORT").astype(float).to_numpy()}
        for _, r in tbl.iterrows():
            if r.PASS:
                good = (have.side != r.side) | (have[r.feature] == r.state_chosen_on_IS)
                specs[f"B2 gate {r.feature} {r.side}"] = good.astype(float).to_numpy()
        for name, w in specs.items():
            x = base * w
            sd = x.std(ddof=1)
            rows.append({
                "variant": name, "slip_bps": slip, "n_days_with_features": len(have), "taken": int((w > 0).sum()),
                "total_R": x.sum(), "mean_R_per_baseline_slot": x.mean(),
                "t": x.mean() / (sd / np.sqrt(len(x))) if sd > 0 else np.nan,
                "diff_vs_baseline_CI95": boot(x - base) if name != "B0 baseline" else "",
            })
    return pd.DataFrame(rows)


def main(path: str) -> None:
    t = pd.read_parquet(path)
    t["day"] = pd.to_datetime(t.day)
    t["year"] = t.day.dt.year
    t["window"] = np.where(t.day < CUT, "IS", "OOS")
    feats = day_features(pd.Index(t.day.unique()))
    t = t.join(feats, on="day")
    t = t[t.F1.notna()].copy()
    print(f"trades with features: {len(t)}  days {t.day.nunique()}  IS {int((t.window == 'IS').sum())}  OOS {int((t.window == 'OOS').sum())}")
    pd.options.display.float_format = _fmt
    tbl, chosen = gate_table(t)
    print("\n== Gate table (state chosen on IS only; OOS judged) ==")
    print(tbl.to_string(index=False))
    tbl.to_csv(RES / "regime_gate_table.csv", index=False)
    v = variants_10y(t, chosen, tbl)
    print("\n== Ten-year variants ==")
    print(v.to_string(index=False))
    v.to_csv(RES / "variants_10y.csv", index=False)
    p = paper_replay(feats, tbl)
    print("\n== Paper replay (67 trades; rows limited to days with features) ==")
    print(p.to_string(index=False))
    p.to_csv(RES / "variants_paper.csv", index=False)
    print(f"\ntrials: features {tbl.feature.nunique()} (x2 sides = {len(tbl)} cells), variants B1 1, B2 {int(tbl.PASS.sum())}, B3 1")


if __name__ == "__main__":
    main(sys.argv[1])
