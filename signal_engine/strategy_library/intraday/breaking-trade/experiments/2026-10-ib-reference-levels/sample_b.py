"""Sample B: H1-H4 on the BREAKINGTRADE baseline replay trades (shadow evidence, ~2 months).

    PYTHONPATH=. uv run python <this dir>/sample_b.py

Trades: ../2026-10-amt-replay/results/amt_B_2026-08-18.csv (P1) and amt_B_2026-09-24.csv (P2),
written by `amt_replay.py B` (entry at the poll minute's 1m open). Features from Historify 1m
bars (read-only); the site's own ib_pct is the latest market_profile snapshot at or before the
entry poll. Costs: the replay's own net4 (Flattrade charges plus 4 bps slippage per side).
"""

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[5]
sys.path.insert(0, str(HERE))
import features as F  # noqa: E402

from signal_engine.timeutils import IST  # noqa: E402

REPLAY = HERE.parent / "2026-10-amt-replay" / "results"
H = duckdb.connect(f"{ROOT}/db/historify.duckdb", read_only=True)
B = sqlite3.connect(f"file:{ROOT}/signal_engine/data/breakingtrade.db?mode=ro", uri=True)
P2_START = "2026-09-24"


def bars(sym, day):
    a = int(dt.datetime.combine(day - dt.timedelta(days=35), dt.time(9, 15), IST).timestamp())
    b = int(dt.datetime.combine(day, dt.time(15, 30), IST).timestamp())
    df = H.execute(
        "select timestamp, open, high, low, close, volume from market_data where symbol=? "
        "and exchange='NSE' and interval='1m' and timestamp between ? and ? order by timestamp",
        [sym, a, b],
    ).df()
    df.index = pd.to_datetime(df.timestamp, unit="s", utc=True).dt.tz_convert(IST)
    return df.rename(columns=str.capitalize)[["Open", "High", "Low", "Close", "Volume"]]


def site_ib_pct(sym, day, poll):
    r = B.execute(
        "select ib_pct from snapshots where kind='market_profile' and symbol=? "
        "and captured_at <= ? and date(captured_at)=? order by captured_at desc limit 1",
        (sym, f"{day} {poll}:00", str(day)),
    ).fetchone()
    return float(r[0]) if r and r[0] not in (None, "") else np.nan


def load():
    df = pd.concat(
        [
            pd.read_csv(REPLAY / "amt_B_2026-08-18.csv"),
            pd.read_csv(REPLAY / "amt_B_2026-09-24.csv"),
        ],
        ignore_index=True,
    )
    df = df.drop_duplicates(["sym", "day", "poll"])
    notional = df.fill * df.qty
    return df.assign(
        g=df.gross / notional * 1e4,
        n=df.net4 / notional * 1e4,
        d=np.where(df.side == "LONG", 1, -1),
    )


def annotate(df, truncate=False):
    rows = []
    for r in df.itertuples():
        day = dt.date.fromisoformat(r.day)
        et = pd.Timestamp(dt.datetime.combine(day, dt.time.fromisoformat(r.poll), IST))
        b = bars(r.sym, day)
        if truncate:
            b = b[b.index < et]
        f = F.SymbolProfile(b, min_session_bars=300).features(day, et, r.d, r.fill, r.stop, r.tp)
        if f is not None:
            rows.append({"idx": r.Index, **f, "site_ib_pct": site_ib_pct(r.sym, r.day, r.poll)})
    return pd.DataFrame(rows).set_index("idx")


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    df = load()
    feats = annotate(df)
    df = df.join(feats, how="inner")
    chk = annotate(df.sample(25, random_state=7), truncate=True)
    cols = ["ib_atr", "n_naked", "nk_target", "nk_stop", "prev_poc", "comp_poc"]
    bad = sum(
        not np.allclose(chk[c].astype(float), df.loc[chk.index, c].astype(float), equal_nan=True)
        for c in cols
    ) + int(
        (chk.open_loc != df.loc[chk.index, "open_loc"]).sum()
        + (chk.h4 != df.loc[chk.index, "h4"]).sum()
    )
    print(f"{len(df)} trades annotated; look-ahead mismatches on 25 truncated recomputes: {bad}")
    p1 = df.day < P2_START
    per = {"P1": p1, "P2": ~p1}
    df["ib_terc"] = F.tercile(df.ib_atr, df.loc[p1, "ib_atr"].quantile([1 / 3, 2 / 3]).to_numpy())
    df["site_terc"] = F.tercile(
        df.site_ib_pct, df.loc[p1, "site_ib_pct"].quantile([1 / 3, 2 / 3]).to_numpy()
    )
    df["nk"] = np.select(
        [df.nk_target & ~df.nk_stop, df.nk_stop & ~df.nk_target, df.nk_target & df.nk_stop],
        ["target_side", "stop_side", "both"],
        "none",
    )
    print(f"all: n {len(df)} gross {df.g.mean():+.1f} net {df.n.mean():+.1f} bps")
    for h, col in (
        ("H1 IB/ATR tercile", "ib_terc"),
        ("H1 site ib_pct tercile", "site_terc"),
        ("H2 open vs prior VA", "open_loc"),
        ("H3 naked POC", "nk"),
        ("H4 entry vs prior/composite POC", "h4"),
    ):
        t = F.bucket_table(df, col, per)
        print(f"\n-- {h}")
        print(
            t[
                [
                    "bucket",
                    "P1_n",
                    "P1_gross",
                    "P1_net",
                    "P1_t",
                    "P2_n",
                    "P2_gross",
                    "P2_net",
                    "P2_t",
                ]
            ].to_string(index=False)
        )
    df.to_csv(HERE / "results" / "sample_b.csv", index=False)
