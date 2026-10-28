"""Build trades.pkl for sample_a.py (unrecorded IDEA-034 grid driver, copied verbatim).

PYTHONPATH=. uv run python <this dir>/inputs_trades.py <dir with frames.pkl>
"""

import dataclasses
import json
import multiprocessing as mp
import pickle
import sys

import numpy as np
import pandas as pd

from signal_engine.backtest import data, metrics
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.costs import recost
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.balance_breakout import BalanceBreakout, BalanceParams
from signal_engine.backtest.types import HOLDOUT_OOS_FRACTION, Ctx, RunConfig

SC = sys.argv[1]
blob = pickle.load(open(f"{SC}/frames.pkl", "rb"))
FRAMES, CFG = blob["frames"], blob["cfg"]
VARIANTS = {
    "V1_N5_k1.5_p30_edge": {"balance_days": 5, "atr_max_multiple": 1.5},
    "V2_N5_k1.0_p30_edge": {"balance_days": 5, "atr_max_multiple": 1.0},
    "V3_N3_k1.5_p30_edge": {"balance_days": 3, "atr_max_multiple": 1.5},
    "V4_N3_k1.0_p30_edge": {"balance_days": 3, "atr_max_multiple": 1.0},
    "V5_N5_k1.5_two5_edge": {"balance_days": 5, "atr_max_multiple": 1.5, "trigger": "two5"},
    "V6_N5_k1.5_p30_mid": {"balance_days": 5, "atr_max_multiple": 1.5, "stop_mode": "mid"},
    "P1_non_balance": {"balance_days": 5, "atr_max_multiple": 1.5, "placebo": "non_balance"},
    "P2_random_entry": {"balance_days": 5, "atr_max_multiple": 1.5, "placebo": "random_entry"},
}
only = sys.argv[2].split(",") if len(sys.argv) > 2 else list(VARIANTS)
run = RunConfig(cost_bps=rr.INTRADAY_COST_BPS).with_(**BalanceBreakout.run_overrides)
run = rr._apply_profile_settings(run, CFG)
run = rr._apply_entry_caps(run, CFG)


def work(sym):
    s = BalanceBreakout()
    out = []
    for name in only:
        p = BalanceParams(**VARIANTS[name])
        ctx = Ctx(s.prepare_for(sym, FRAMES[sym], p), symbol=sym)
        out += [(name, t) for t in simulate(ctx, s, p, run)]
    return out


if __name__ == "__main__":
    syms = sorted(FRAMES)
    with mp.get_context("fork").Pool(10) as pool:
        res = pool.map(work, syms, chunksize=4)
    days = data.sessions(FRAMES)
    cut = days[int(len(days) * (1 - HOLDOUT_OOS_FRACTION))]
    model = rr._primary_cost_model("intraday", CFG)
    allt = {n: [] for n in only}
    for lst in res:
        for n, t in lst:
            allt[n].append(t)
    pickle.dump(
        {"trades": allt, "cut": cut, "n_sessions": len(days), "first": days[0], "last": days[-1]},
        open(f"{SC}/trades.pkl", "wb"),
    )

    def pf(r):
        w, loss = r[r > 0].sum(), -r[r <= 0].sum()
        return round(w / loss, 3) if loss > 0 else np.nan

    rows = []
    for n in only:
        ts = recost(allt[n], model)
        for w, f in (
            ("ALL", lambda d: True),
            ("IS", lambda d: d < cut),
            ("OOS", lambda d: d >= cut),
        ):
            sub = [t for t in ts if f(t.day)]
            if not sub:
                rows.append({"variant": n, "win": w, "n": 0})
                continue
            r = np.array([t.r_net for t in sub])
            dd = np.array([t.day for t in sub])
            net_bps = np.array([t.net_bps for t in sub])
            gr = np.array([t.gross_bps for t in sub])
            m = metrics.summary(sub)
            rows.append(
                {
                    "variant": n,
                    "win": w,
                    "n": len(sub),
                    "gross_bps": round(gr.mean(), 2),
                    "net_bps": round(net_bps.mean(), 2),
                    "net_R": m["net_R"],
                    "t_R": m["t"],
                    "t_bps": round(metrics.clustered_t(net_bps, dd), 2),
                    "pf": pf(r),
                    "maxdd_R": m["max_dd_R"],
                    "win_pct": m["win"],
                    "long": m["long_trades"],
                    "short": m["short_trades"],
                    "stop_pct": m["stop_pct"],
                }
            )
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print(
        f"cut={cut} sessions={len(days)} {days[0]}..{days[-1]}  cost round trip (mean bps): see recost"
    )
    print(df.to_string(index=False))
    df.to_csv(f"{SC}/grid.csv", index=False)
