"""CLI for the backtest framework.

    uv run --group analysis python -m signal_engine.backtest ema9
    uv run --group analysis python -m signal_engine.backtest ema9 --full
"""

from __future__ import annotations

import argparse

import pandas as pd

from signal_engine.backtest import data
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.types import RunConfig

REGISTRY: dict = {}


def _register():
    from signal_engine.backtest.strategies.ema9 import Ema9, Ema9Params
    REGISTRY["ema9"] = (Ema9, Ema9Params)


def main() -> None:
    _register()
    ap = argparse.ArgumentParser(prog="signal_engine.backtest")
    ap.add_argument("strategy", choices=sorted(REGISTRY))
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--period", default="60d")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--refresh", action="store_true", help="re-download bar data")
    ap.add_argument("--full", action="store_true", help="also print per-symbol and exit mix")
    args = ap.parse_args()

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)

    cls, params_cls = REGISTRY[args.strategy]
    frames = data.load(period=args.period, interval=args.interval, refresh=args.refresh)
    bt = Backtest(cls(), frames, RunConfig(cost_bps=args.cost_bps))
    p = params_cls()

    print(bt.describe())
    print("\n=== shipped defaults, in-sample vs out-of-sample ===")
    print(bt.confirm(p, args.strategy).to_string(index=False))
    print("\n=== cost sensitivity (full period) ===")
    print(bt.cost_sensitivity(p).to_string(index=False))

    if args.full:
        print("\n=== per symbol ===")
        ps = bt.by_symbol(p)
        print(f"{len(ps)} traded, {int((ps.total_R > 0).sum())} profitable, "
              f"median {ps.net_R.median():+.3f}R")
        print(ps.to_string(index=False))
        print("\n=== exit mix ===")
        print(bt.by_reason(p).to_string())

    print("\nRead the OOS row, not the IS row. |t| < 2 means indistinguishable from zero.")


if __name__ == "__main__":
    main()
