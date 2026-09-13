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
    from signal_engine.backtest.strategies.dhb import Dhb, DhbParams
    from signal_engine.backtest.strategies.ema9 import Ema9, Ema9Params
    from signal_engine.backtest.strategies.ema9_pdf import Ema9Pdf, Ema9PdfParams
    from signal_engine.backtest.strategies.ema9_vwap import Ema9Vwap, Ema9VwapParams
    from signal_engine.backtest.strategies.gap_rsi import GapRsi, GapRsiParams
    from signal_engine.backtest.strategies.ib_extension import IbExtension, IbExtParams
    from signal_engine.backtest.strategies.key_level import KeyLevel, KeyLevelParams
    from signal_engine.backtest.strategies.orb import Orb, OrbParams
    from signal_engine.backtest.strategies.phoenix import Phoenix, PhoenixParams
    from signal_engine.backtest.strategies.value_zone import ValueZone, ValueZoneParams
    REGISTRY["dhb"] = (Dhb, DhbParams, "intraday")
    REGISTRY["ema9"] = (Ema9, Ema9Params, "intraday")
    REGISTRY["ema9_pdf"] = (Ema9Pdf, Ema9PdfParams, "intraday")
    REGISTRY["ema9_vwap"] = (Ema9Vwap, Ema9VwapParams, "intraday")
    REGISTRY["gap_rsi"] = (GapRsi, GapRsiParams, "swing")
    REGISTRY["ib_extension"] = (IbExtension, IbExtParams, "intraday")
    REGISTRY["key_level"] = (KeyLevel, KeyLevelParams, "intraday")
    REGISTRY["orb"] = (Orb, OrbParams, "intraday")
    REGISTRY["phoenix"] = (Phoenix, PhoenixParams, "swing")
    REGISTRY["value_zone"] = (ValueZone, ValueZoneParams, "swing")


def main() -> None:
    _register()
    ap = argparse.ArgumentParser(prog="signal_engine.backtest")
    ap.add_argument("strategy", choices=sorted(REGISTRY))
    ap.add_argument("--source", default="historify", choices=["historify", "yahoo"],
                    help="historify (default) = OpenAlgo's own store: years of broker "
                         "1m bars resampled for intraday, split-adjusted broker daily "
                         "bars for swing, no external rate limit; yahoo = fallback "
                         "only (60 days of 5m intraday, or 10y adjusted daily)")
    ap.add_argument("--interval", default=None, help="default: 5m intraday, 1d swing")
    ap.add_argument("--period", default=None, help="default: 60d intraday, 10y swing")
    ap.add_argument("--cost-bps", type=float, default=None,
                    help="default: 16 bps intraday, 24 bps swing")
    ap.add_argument("--refresh", action="store_true", help="re-download bar data")
    ap.add_argument("--full", action="store_true", help="also print per-symbol and exit mix")
    ap.add_argument("--trials", type=int, default=1,
                    help="how many configurations were searched in total, across every "
                         "strategy and sweep. Sets the |t| a result must clear.")
    args = ap.parse_args()

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)

    cls, params_cls, kind = REGISTRY[args.strategy]
    swing = kind == "swing"
    interval = args.interval or ("1d" if swing else "5m")
    period = args.period or ("10y" if swing else "60d")
    cost_bps = args.cost_bps if args.cost_bps is not None else (24.0 if swing else 16.0)

    if swing:
        # daily bars must be split/bonus adjusted - a 1:5 split reads as a -80% gap.
        # Historify is the default: it is broker-verified, carries no per-symbol rate
        # limit, and its daily coverage reaches further back than its 1-minute
        # coverage. `_split_adjust_daily` gives it the same adjustment guarantee
        # `auto_adjust=True` gets from yfinance. `--source yahoo` stays available as a
        # fallback for a symbol Historify has not been backfilled for yet.
        if args.source == "historify":
            frames = data.from_historify_daily(min_sessions=500)
        else:
            frames = data.load(period=period, interval=interval, refresh=args.refresh,
                               min_bars=750, auto_adjust=True)
        bt = Backtest(cls(), frames, SwingRunConfig(cost_bps=cost_bps),
                      engine=simulate_swing)
    elif args.source == "historify":
        frames = data.from_historify(interval=interval)
        strategy = cls()
        run = RunConfig(cost_bps=cost_bps).with_(**getattr(strategy, "run_overrides", {}))
        bt = Backtest(strategy, frames, run)
    else:
        frames = data.load(period=period, interval=interval, refresh=args.refresh)
        strategy = cls()
        run = RunConfig(cost_bps=cost_bps).with_(**getattr(strategy, "run_overrides", {}))
        bt = Backtest(strategy, frames, run)
    p = params_cls()

    print(bt.describe())
    print("\n=== shipped defaults, in-sample vs out-of-sample ===")
    print(bt.confirm(p, args.strategy).to_string(index=False))
    print("\n=== cost sensitivity (full period) ===")
    bps = (0, 10, 20, 24, 35) if swing else (0, 8, 16, 24)
    print(bt.cost_sensitivity(p, bps=bps).to_string(index=False))

    if swing:
        print("\n=== by direction (full period) ===")
        for lbl, kw in (("longs only", {"allow_shorts": False}),
                        ("shorts only", {"allow_longs": False})):
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

    hurdle = metrics.hurdle_t(args.trials)
    print("\nRead the OOS row, not the IS row.")
    print("`t` is clustered by session - trades taken across a basket on one day are "
          "one market bet,\nnot forty. `t_naive` is the uncorrected figure, shown so "
          "the gap is visible.")
    print(f"With {args.trials} configuration(s) searched, a result needs |t| > {hurdle:.2f} "
          f"to beat chance.")
    if args.source == "yahoo":
        print("\nSOURCE IS YAHOO: ~59 sessions of 5m bars, and roughly half of them are "
              "missing\ntheir closing bars. Indicative only - rerun with --source "
              "historify before believing it.")


if __name__ == "__main__":
    main()
