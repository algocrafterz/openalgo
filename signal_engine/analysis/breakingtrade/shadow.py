"""Paper results of the BreakingTrade strategies, split for comparison.

    uv run python -m signal_engine.analysis.breakingtrade.shadow [--since YYYY-MM-DD]
        [--slippage-bps 4] [--broker flattrade]

Each closed paper trade (signal_engine db.fetch_day_trades, which already counts partial legs
once) is matched to the scanner's trade_signal alert for the same strategy, symbol and day, and
grouped by strategy, by the range-integrity flag recorded on the signal (TradePlan.range_integrity,
recorded from 2026-10-28) and by the scan that picked the name. A trade with no recorded flag
or scan is "unknown"; older trades always are.

Net = engine P&L minus estimated statutory charges and a slippage allowance on both sides. The
sandbox fills at the last traded price, so the engine P&L alone carries no spread or impact;
the allowance is an assumption, not a measurement. Expectancy is net P&L over the planned risk
(|entry - stop| x quantity). Read-only on trades.db and breakingtrade.db.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, timedelta

from signal_engine import db
from signal_engine.analysis.breakingtrade import alerts
from signal_engine.timeutils import ist_today

STRATEGIES = ("BREAKINGTRADE", "BREAKINGTRADE-RI")


def summarise(trades: list, tags: dict, cost_of: Callable[[dict], float]) -> dict:
    """Group (day, trade) pairs by strategy, by range-integrity flag and by scan.

    tags maps (strategy, symbol, day) to (flag 0/1/None, scan name or None).
    """
    groups: dict[str, dict[str, list]] = {"strategy": {}, "range_integrity": {}, "scan": {}}
    for day, trade in trades:
        flag, scan = tags.get((trade["strategy"], trade["symbol"], day), (None, None))
        flag_name = {1: "pass", 0: "fail"}.get(flag, "unknown")
        risk = abs(trade["entry"] - trade["sl"]) * (trade["quantity"] or 0)
        row = (trade["total_pnl"] - cost_of(trade), risk)
        groups["strategy"].setdefault(trade["strategy"], []).append(row)
        groups["range_integrity"].setdefault(flag_name, []).append(row)
        groups["scan"].setdefault(scan or "unknown", []).append(row)
    return {key: [_stats(name, rows) for name, rows in sorted(by.items())] for key, by in groups.items()}


def _stats(name: str, rows: list) -> dict:
    nets = [net for net, _ in rows]
    r_values = [net / risk for net, risk in rows if risk > 0]
    wins = sum(n for n in nets if n > 0)
    losses = -sum(n for n in nets if n <= 0)
    return {
        "group": name,
        "trades": len(rows),
        "net": sum(nets),
        "win_rate": sum(n > 0 for n in nets) / len(nets),
        "profit_factor": wins / losses if losses else None,
        "exp_r": sum(r_values) / len(r_values) if r_values else None,
    }


def signal_tags(since: str) -> dict:
    """(strategy, symbol, day) -> (range_integrity, scan) from the first signal of that day."""
    with alerts._connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(strategy, scan), symbol, date(created_at), range_integrity, scan "
            "FROM alerts WHERE kind = 'trade_signal' AND date(created_at) >= ? "
            "ORDER BY created_at",
            (since,),
        ).fetchall()
    tags: dict = {}
    for strategy, symbol, day, flag, scan in rows:
        known_scan = None if scan in STRATEGIES else scan
        tags.setdefault((strategy, symbol, day), (flag, known_scan))
    return tags


def paper_trades(since: str, until: date) -> list:
    out = []
    day = date.fromisoformat(since)
    while day <= until:
        for trade in db.fetch_day_trades("analyze", day.isoformat()):
            if trade["strategy"] in STRATEGIES:
                out.append((day.isoformat(), trade))
        day += timedelta(days=1)
    return out


def cost_model(broker: str, slippage_bps: float) -> Callable[[dict], float]:
    from portfolio.costs import india_intraday

    schedule = india_intraday("NSE", broker=broker)

    def cost_of(trade: dict) -> float:
        notional = abs(trade["entry"] * (trade["quantity"] or 0))
        charges = float(schedule.charge(buy_value=notional, sell_value=notional, orders=2))
        return charges + 2 * slippage_bps / 1e4 * notional

    return cost_of


def _print(title: str, rows: list) -> None:
    print(f"\n{title}")
    print(f"  {'group':32} {'trades':>6} {'net Rs':>10} {'win%':>6} {'PF':>6} {'exp R':>7}")
    for r in rows:
        pf = f"{r['profit_factor']:.2f}" if r["profit_factor"] is not None else "-"
        exp_r = f"{r['exp_r']:+.3f}" if r["exp_r"] is not None else "-"
        print(f"  {r['group']:32} {r['trades']:>6} {r['net']:>10,.0f} "
              f"{100 * r['win_rate']:>5.0f}% {pf:>6} {exp_r:>7}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="2026-10-28")
    parser.add_argument("--slippage-bps", type=float, default=4.0)
    parser.add_argument("--broker", default="flattrade")
    args = parser.parse_args()
    trades = paper_trades(args.since, ist_today())
    rows = summarise(trades, signal_tags(args.since), cost_model(args.broker, args.slippage_bps))
    print(f"BreakingTrade paper trades since {args.since}: {len(trades)} "
          f"(net of {args.broker} charges and {args.slippage_bps:g} bps slippage per side)")
    _print("By strategy", rows["strategy"])
    _print("By range-integrity flag (signal recorded it from 2026-10-28)", rows["range_integrity"])
    _print("By scan", rows["scan"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
