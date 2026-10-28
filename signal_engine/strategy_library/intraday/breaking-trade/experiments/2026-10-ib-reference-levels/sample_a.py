"""Sample A: H1-H4 on the IDEA-034 v1 trades and the P1 non-balance placebo, 2017-2026.

    uv run python <this dir>/sample_a.py <dir holding frames.pkl and trades.pkl>

frames.pkl comes from inputs_frames.py (published dataset nse-intraday-5m-20261021-002 through
the run system's data layer); trades.pkl from inputs_trades.py (same engine and run settings as
the recorded run). Costs: the run's primary model (Flattrade plus 4 bps slippage per side).
"""

import multiprocessing as mp
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import features as F  # noqa: E402

from signal_engine.backtest import record_run as rr  # noqa: E402
from signal_engine.backtest.costs import recost  # noqa: E402

SETS = ("V1_N5_k1.5_p30_edge", "P1_non_balance")
SC = Path(sys.argv[1])
FRAMES = pickle.load(open(SC / "frames.pkl", "rb"))
CFG, FRAMES = FRAMES["cfg"], FRAMES["frames"]
TR = pickle.load(open(SC / "trades.pkl", "rb"))
CUT = pd.Timestamp(TR["cut"])
MODEL = rr._primary_cost_model("intraday", CFG)


def trade_frame(name):
    return pd.DataFrame(
        [
            {
                "set": name,
                "sym": t.symbol,
                "day": t.day,
                "et": t.entry_time,
                "d": t.direction,
                "entry": t.entry,
                "sl": t.sl,
                "tp": t.tp,
                "g": t.gross_bps,
                "n": t.net_bps,
            }
            for t in recost(TR["trades"][name], MODEL)
        ]
    )


def annotate(args):
    sym, rows = args
    prof = F.SymbolProfile(FRAMES[sym], min_session_bars=60)
    out = []
    for r in rows.itertuples():
        f = prof.features(r.day, r.et, r.d, r.entry, r.sl, r.tp)
        if f is not None:
            out.append({"idx": r.Index, **f})
    return out


def lookahead_check(df, n=60, seed=7):
    """Recompute features with the symbol's bars truncated before entry; must match exactly."""
    bad = 0
    for r in df.sample(n, random_state=seed).itertuples():
        bars = FRAMES[r.sym]
        prof = F.SymbolProfile(bars[bars.index < r.et], min_session_bars=60)
        f = prof.features(r.day, r.et, r.d, r.entry, r.sl, r.tp)
        for k in (
            "ib_atr",
            "open_loc",
            "n_naked",
            "nk_target",
            "nk_stop",
            "prev_poc",
            "comp_poc",
            "h4",
        ):
            a, b = f[k], getattr(r, k)
            same = (a == b) or (isinstance(a, float) and np.isclose(a, b, equal_nan=True))
            bad += not same
    return bad


def report(df, name):
    is_ = pd.to_datetime(df.day) < CUT
    edges = df.loc[is_, "ib_atr"].quantile([1 / 3, 2 / 3]).to_numpy()
    df["ib_terc"] = F.tercile(df.ib_atr, edges)
    df["nk"] = np.select(
        [df.nk_target & ~df.nk_stop, df.nk_stop & ~df.nk_target, df.nk_target & df.nk_stop],
        ["target_side", "stop_side", "both"],
        "none",
    )
    per = {"IS": is_, "OOS": ~is_}
    sides = {"L": df.d == 1, "S": df.d == -1}
    print(f"\n######## {name}: {len(df)} trades, IB/ATR tercile edges (IS) {edges.round(3)}")
    print(f"all: gross {df.g.mean():+.2f} net {df.n.mean():+.2f}")
    for h, col in (
        ("H1 IB width", "ib_terc"),
        ("H2 open vs prior VA", "open_loc"),
        ("H3 naked POC", "nk"),
        ("H4 entry vs prior/composite POC", "h4"),
    ):
        t = F.bucket_table(df, col, per)
        s = F.bucket_table(df, col, sides)
        print(f"\n-- {h}")
        print(
            t[
                [
                    "bucket",
                    "IS_n",
                    "IS_gross",
                    "IS_net",
                    "IS_t",
                    "OOS_n",
                    "OOS_gross",
                    "OOS_net",
                    "OOS_t",
                ]
            ].to_string(index=False)
        )
        print(
            s[["bucket", "L_n", "L_gross", "L_net", "S_n", "S_gross", "S_net"]].to_string(
                index=False
            )
        )
    return df


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    trades = pd.concat([trade_frame(n) for n in SETS], ignore_index=True)
    with mp.get_context("fork").Pool(10) as pool:
        res = pool.map(annotate, list(trades.groupby("sym")), chunksize=2)
    feats = pd.DataFrame([x for lst in res for x in lst]).set_index("idx")
    df = trades.join(feats, how="inner")
    print(f"annotated {len(df)} of {len(trades)} trades")
    print("look-ahead mismatches (60 sampled trades):", lookahead_check(df))
    for n in SETS:
        out = report(df[df.set == n].copy(), n)
        out.to_csv(HERE / "results" / f"sample_a_{n}.csv", index=False)
