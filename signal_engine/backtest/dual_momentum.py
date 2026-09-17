"""Dual momentum (Antonacci) - relative rank + absolute filter, vs plain relative.

    uv run --group analysis python -m signal_engine.backtest.dual_momentum

Source: Gary Antonacci's "dual momentum" - the SAME 12-1 relative-momentum ranking
this repo already validated for momentum-rank (see
`pinescripts/swing/momentum-rank/STRATEGY-ANALYSIS.md`), plus one addition: a pick
also needs POSITIVE absolute momentum (its own trailing return > 0), not just top-N
rank. A name that is merely the best of a falling universe is excluded and that
slot sits in cash instead. The published claim (Antonacci's 40-year US backtest) is
similar returns to plain momentum with under half the drawdown - this script checks
whether that holds on this platform's own broker-verified NSE data, using the
SAME methodology, universe, cost model and IS/OOS split already adopted for
momentum-rank, so the two are directly comparable.

This is a NEW candidate, run alongside momentum-rank, not a replacement for it -
momentum-rank remains the proven, paper-tracked strategy. This script exists to
find out whether the absolute filter is worth adding as a second, independent
positional strategy or a variant of the first.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import pandas as pd

from signal_engine.backtest import data
from signal_engine.backtest.portfolio import PortfolioBacktest, PortfolioConfig, build_factor


def main() -> None:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.dual_momentum")
    # defaults match the ADOPTED momentum-rank config (STRATEGY-ANALYSIS.md,
    # 2026-09-15 "full-universe fine-tune" entry), for direct comparability.
    ap.add_argument("--lookback", type=int, default=300)
    ap.add_argument("--skip", type=int, default=21)
    ap.add_argument("--top-n", type=int, default=12)
    ap.add_argument("--rebal", type=int, default=30)
    ap.add_argument("--cost-bps", type=float, default=22.0)
    ap.add_argument("--min-price", type=float, default=20.0)
    args = ap.parse_args()

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)

    frames = data.from_historify_daily(min_sessions=500)
    cfg = PortfolioConfig(rebalance_days=args.rebal, top_n=args.top_n,
                          cost_bps=args.cost_bps, min_price=args.min_price)
    pb = PortfolioBacktest(frames, cfg)
    print(pb.describe())
    print(f"factor: mom_skip, lookback={args.lookback}, skip={args.skip}\n")

    factor = build_factor(pb.panel, "mom_skip", args.lookback, args.skip)

    print("=== relative momentum only (momentum-rank's existing methodology) ===")
    plain = pb.report(factor, "relative-only")
    print(plain.to_string(index=False))

    dual_cfg = replace(cfg, require_positive=True)
    print("\n=== dual momentum: relative rank + positive absolute-momentum filter ===")
    dual = pb.report(factor, "dual", cfg=dual_cfg)
    print(dual.to_string(index=False))

    print("\n=== how often the absolute filter actually bites (cash fraction per rebalance) ===")
    r_dual = pb.run(factor, dual_cfg)
    print("relative-only : 0% cash by construction (no absolute filter applied)")
    print(f"dual momentum : cash held {100 * float(r_dual['cash_frac'].mean()):.1f}% of book-months "
          f"on average, max {100 * float(r_dual['cash_frac'].max()):.1f}% in the worst single rebalance")
    worst = r_dual["cash_frac"].nlargest(5)
    print("\nrebalances with the most cash (biggest market-stress periods this filter caught):")
    print(worst.to_string())

    print("\n=== max drawdown comparison, full period ===")
    print(f"relative-only max DD: {plain[plain.label.str.contains('ALL')].max_dd.iloc[0]:.1f}%")
    print(f"dual momentum max DD: {dual[dual.label.str.contains('ALL')].max_dd.iloc[0]:.1f}%")
    print("\nRead the OOS row, not the IS row, for both tables above - same caveat as every "
          "other backtest here: this is ONE parameter configuration (the one already adopted "
          "for momentum-rank), not a swept grid, so treat this as a first look, not a final "
          "verdict.")


if __name__ == "__main__":
    main()
