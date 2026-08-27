"""CLI for the backtest framework.

    uv run --group analysis python -m signal_engine.backtest ema9
    uv run --group analysis python -m signal_engine.backtest ema9 --full
    uv run --group analysis python -m signal_engine.backtest gap_rsi --full
"""

from __future__ import annotations

import argparse

import pandas as pd

from signal_engine.backtest import data, metrics
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import RunConfig

#: name -> (strategy class, params class, kind). "kind" picks the simulator and the
#: bar data: intraday strategies run on 5-minute bars and flatten at the close, swing
#: strategies run on adjusted daily bars and hold for weeks.
REGISTRY: dict = {}


def _register():
    from signal_engine.backtest.strategies.ema9 import Ema9, Ema9Params
    from signal_engine.backtest.strategies.gap_rsi import GapRsi, GapRsiParams
    REGISTRY["ema9"] = (Ema9, Ema9Params, "intraday")
    REGISTRY["gap_rsi"] = (GapRsi, GapRsiParams, "swing")


def main() -> None:
    _register()
    ap = argparse.ArgumentParser(prog="signal_engine.backtest")
    ap.add_argument("strategy", choices=sorted(REGISTRY))
    ap.add_argument("--interval", default=None, help="default: 5m intraday, 1d swing")
    ap.add_argument("--period", default=None, help="default: 60d intraday, 10y swing")
    ap.add_argument("--cost-bps", type=float, default=None,
                    help="default: 10 bps intraday, 20 bps swing")
    ap.add_argument("--refresh", action="store_true", help="re-download bar data")
    ap.add_argument("--full", action="store_true", help="also print per-symbol and exit mix")
    args = ap.parse_args()

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)

    cls, params_cls, kind = REGISTRY[args.strategy]
    swing = kind == "swing"
    interval = args.interval or ("1d" if swing else "5m")
    period = args.period or ("10y" if swing else "60d")
    cost_bps = args.cost_bps if args.cost_bps is not None else (20.0 if swing else 10.0)

    if swing:
        # daily bars must be split/bonus adjusted - a 1:5 split reads as a -80% gap
        frames = data.load(period=period, interval=interval, refresh=args.refresh,
                           min_bars=750, auto_adjust=True)
        bt = Backtest(cls(), frames, SwingRunConfig(cost_bps=cost_bps),
                      engine=simulate_swing)
    else:
        frames = data.load(period=period, interval=interval, refresh=args.refresh)
        bt = Backtest(cls(), frames, RunConfig(cost_bps=cost_bps))
    p = params_cls()

    print(bt.describe())
    print("\n=== shipped defaults, in-sample vs out-of-sample ===")
    print(bt.confirm(p, args.strategy).to_string(index=False))
    print("\n=== cost sensitivity (full period) ===")
    bps = (0, 8, 15, 20, 30) if swing else (6, 8, 10, 12)
    print(bt.cost_sensitivity(p, bps=bps).to_string(index=False))

    if swing:
        print("\n=== by direction (full period) ===")
        for lbl, kw in (("longs only", dict(allow_shorts=False)),
                        ("shorts only", dict(allow_longs=False))):
            print(pd.DataFrame([metrics.summary(
                bt.trades(p, "all", run=bt.run.with_(**kw)), lbl)]).to_string(index=False))

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
