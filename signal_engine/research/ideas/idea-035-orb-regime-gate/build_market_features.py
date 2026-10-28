"""Per-day market regime features for the ORB regime study, from the published 5m dataset.

Every feature is known at or before 09:45 IST on the day it is used (the earliest ORB15 entry),
so a gate built on it is causal. Cross-sectional medians over the eligible F&O universe stand in
for the index, because Historify's NIFTY daily history is thin before 2021.

Run:  PYTHONPATH=. uv run python signal_engine/research/ideas/idea-035-orb-regime-gate/build_market_features.py <scratch_dir>
Output: results/market_day_features.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest import mfe_mae_report as M

HERE = Path(__file__).resolve().parent
M.DATASET = "nse-intraday-5m-20261021-002"


def per_symbol_days(df: pd.DataFrame) -> pd.DataFrame:
    d = ind.add_session_columns(df)
    g = d.groupby("day")
    first = g.head(1).set_index("day")
    open_ = first["Open"]
    rows = pd.DataFrame({"open": open_})
    rows["high"] = g["High"].max()
    rows["low"] = g["Low"].min()
    rows["close"] = g["Close"].last()
    inside = d[d["from_open"] < 30]  # 09:15 to 09:44 bars, closes by 09:45
    rows["c0945"] = inside.groupby("day")["Close"].last()
    orb = d[d["from_open"] < 15]
    rows["orb_rng"] = orb.groupby("day")["High"].max() - orb.groupby("day")["Low"].min()
    return rows


def main(scratch: Path) -> None:
    frames, _prov, _profile = M.load_published(scratch)
    parts = []
    for sym, df in frames.items():
        r = per_symbol_days(df)
        r["symbol"] = sym
        parts.append(r.reset_index())
    a = pd.concat(parts, ignore_index=True)
    a = a.sort_values(["symbol", "day"])
    a["prev_close"] = a.groupby("symbol")["close"].shift(1)
    a["prev_high"] = a.groupby("symbol")["high"].shift(1)
    a["prev_low"] = a.groupby("symbol")["low"].shift(1)
    a["prev_ret"] = a.groupby("symbol")["close"].pct_change().groupby(a["symbol"]).shift(1)
    a["gap"] = a["open"] / a["prev_close"] - 1
    a["drive"] = a["c0945"] / a["open"] - 1  # opening drive, 09:15 to 09:45
    a["prev_rng"] = (a["prev_high"] - a["prev_low"]) / a["prev_close"]
    a["rng_avg20"] = a.groupby("symbol")["prev_rng"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    a["rng_exp"] = a["prev_rng"] / a["rng_avg20"]
    a["above_sma20"] = a.groupby("symbol")["close"].transform(lambda s: (s > s.rolling(20).mean()).astype(float))
    a["above_sma20_prev"] = a.groupby("symbol")["above_sma20"].shift(1)
    # discard split and data artefacts: a one-day move beyond 15 percent is not a market move
    for c in ("gap", "drive", "prev_ret"):
        a.loc[a[c].abs() > 0.15, c] = np.nan
    out = a.groupby("day").agg(
        n_sym=("symbol", "nunique"),
        gap_med=("gap", "median"),
        drive_med=("drive", "median"),
        prev_ret_med=("prev_ret", "median"),
        prev_breadth=("prev_ret", lambda s: (s > 0).mean()),
        rng_exp_med=("rng_exp", "median"),
        above_sma20_breadth=("above_sma20_prev", "mean"),
    )
    out.index = pd.to_datetime(out.index).date
    out.index.name = "day"
    out.to_csv(HERE / "results" / "market_day_features.csv")
    print(out.describe().T.to_string())


if __name__ == "__main__":
    main(Path(sys.argv[1]))
