#!/usr/bin/env python
"""Paper-trading ledger for momentum-rank - track what following the live digest
would actually have earned, without placing real orders.

WHY THIS EXISTS

momentum_rank_strategy.py only ever sends a digest - it never places an order and
never records what a trader who followed it would now be holding or what they would
have made. This module is that missing bookkeeping: a persistent ledger, updated once
per real rebalance, so performance can be judged against real fills instead of a
backtest assumption.

METHODOLOGY (mirrors the live digest's own instructions, not a fresh backtest)

- Equal-weight, `TOP_N` slots, same as the live script's "~8.3% of capital each."
- A HOLD name (still in the top_n) is left untouched - no re-weighting trade, exactly
  like the digest's "HOLD, no action" line. Only SELL/BUY names are actually traded.
- A BUY is sized off the CURRENT total portfolio value / TOP_N at the time of that
  rebalance (cash + mark-to-market of names staying held) - not off the original
  capital - so a new entry after some drift still targets an equal-weight slot of
  the book as it now stands.
- Fill price is the next trading session's OPEN after the rebalance signal, per
  STRATEGY-ANALYSIS.md's documented convention (a daily alert fires after 15:30, so a
  market order fills at the next open). Quantity is floor(allocation / price) - whole
  shares only, CNC delivery.

USAGE

    # initialize or advance the ledger to match momentum_rank_state.json's current
    # holding, fetching real prices via the OpenAlgo client
    uv run python momentum_rank_paper_tracker.py --sync

    # print current positions, closed trades, and P&L
    uv run python momentum_rank_paper_tracker.py --report
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

CAPITAL = 100_000.0
TOP_N = 12
LEDGER_PATH = Path(os.environ.get(
    "MOMENTUM_PAPER_LEDGER_PATH", "log/strategies/momentum_rank_paper_ledger.json"))
STATE_PATH = Path(os.environ.get(
    "MOMENTUM_STATE_PATH", "log/strategies/momentum_rank_state.json"))


# ---------------------------------------------------------------------------
# Pure ledger math (no I/O) - unit tested directly.
# ---------------------------------------------------------------------------


def new_ledger(capital: float = CAPITAL) -> dict:
    return {
        "capital": capital,
        "cash": capital,
        "positions": {},   # symbol -> {qty, entry_price, entry_date}
        "closed_trades": [],
        "last_synced_rebalance_count": 0,
    }


def _warn_if_unfunded(sym: str, qty: int, price: float, per_slot: float) -> None:
    if qty == 0:
        print(f"[paper-tracker] WARNING: {sym} @ Rs {price:,.2f} costs more than "
              f"the Rs {per_slot:,.2f} per-slot allocation - bought 0 shares. "
              f"This position is completely unfunded at this capital level, "
              f"not just underweight.")


def initial_buy(ledger: dict, as_of: str, target: list[str],
                fill_prices: dict[str, float], top_n: int = TOP_N) -> dict:
    """First-ever rebalance: split `ledger['cash']` equally across `target`."""
    per_slot = ledger["cash"] / top_n
    positions = dict(ledger["positions"])
    cash = ledger["cash"]
    for sym in target:
        price = fill_prices[sym]
        qty = int(per_slot // price)
        _warn_if_unfunded(sym, qty, price, per_slot)
        cash -= qty * price
        positions[sym] = {"qty": qty, "entry_price": price, "entry_date": as_of}
    return {**ledger, "positions": positions, "cash": cash}


def apply_rebalance(ledger: dict, as_of: str, sells: list[str], buys: list[str],
                    target: list[str], fill_prices: dict[str, float],
                    top_n: int = TOP_N) -> dict:
    """Sell dropped names, buy new names at an equal-weight slot of the CURRENT
    book value, leave continuing (HOLD) names untouched - mirrors the digest's
    own SELL/BUY/HOLD instructions exactly, not a full every-period reweight.
    """
    positions = dict(ledger["positions"])
    closed = list(ledger["closed_trades"])
    cash = ledger["cash"]

    for sym in sells:
        pos = positions.pop(sym)
        exit_price = fill_prices[sym]
        proceeds = pos["qty"] * exit_price
        cash += proceeds
        cost_basis = pos["qty"] * pos["entry_price"]
        pnl = proceeds - cost_basis
        closed.append({
            "symbol": sym, "qty": pos["qty"],
            "entry_date": pos["entry_date"], "entry_price": pos["entry_price"],
            "exit_date": as_of, "exit_price": exit_price,
            "pnl": pnl, "pnl_pct": (pnl / cost_basis * 100.0) if cost_basis else 0.0,
        })

    # Total book value AFTER selling, valuing remaining (HOLD) positions at
    # today's fill prices where known, else their last entry price.
    remaining_value = sum(
        pos["qty"] * fill_prices.get(sym, pos["entry_price"])
        for sym, pos in positions.items())
    total_value = cash + remaining_value
    per_slot = total_value / top_n

    for sym in buys:
        price = fill_prices[sym]
        qty = int(per_slot // price)
        _warn_if_unfunded(sym, qty, price, per_slot)
        cash -= qty * price
        positions[sym] = {"qty": qty, "entry_price": price, "entry_date": as_of}

    return {**ledger, "positions": positions, "cash": cash, "closed_trades": closed}


def portfolio_value(ledger: dict, current_prices: dict[str, float]) -> float:
    market_value = sum(
        pos["qty"] * current_prices.get(sym, pos["entry_price"])
        for sym, pos in ledger["positions"].items())
    return ledger["cash"] + market_value


def summary(ledger: dict, current_prices: dict[str, float]) -> dict:
    total = portfolio_value(ledger, current_prices)
    realized = sum(t["pnl"] for t in ledger["closed_trades"])
    unrealized = sum(
        pos["qty"] * (current_prices.get(sym, pos["entry_price"]) - pos["entry_price"])
        for sym, pos in ledger["positions"].items())
    return {
        "capital": ledger["capital"],
        "total_value": round(total, 2),
        "cash": round(ledger["cash"], 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_return_pct": round((total - ledger["capital"]) / ledger["capital"] * 100, 2),
        "open_positions": len(ledger["positions"]),
        "closed_trades": len(ledger["closed_trades"]),
    }


# ---------------------------------------------------------------------------
# I/O - ledger persistence, live state, and price fetch.
# ---------------------------------------------------------------------------


def load_ledger(path: Path = LEDGER_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return new_ledger()


def save_ledger(ledger: dict, path: Path = LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2))


def _load_live_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        raise RuntimeError(f"{path} not found - the live strategy has not run yet")
    return json.loads(path.read_text())


def _fetch_open_prices(client, symbols: list[str], as_of: str,
                       exchange: str = "NSE") -> dict[str, float]:
    """Each symbol's OPEN on `as_of` (YYYY-MM-DD) - the fill-price convention.

    Today's open is fixed at 9:15 AM IST and does not move for the rest of the
    session, so this is safe to call even before today's close (unlike close,
    which the live script's own pre-settlement guard has to protect against).
    """
    prices = {}
    for sym in symbols:
        df = client.history(symbol=sym, exchange=exchange, interval="D",
                            start_date=as_of, end_date=as_of)
        if isinstance(df, dict) or df is None or df.empty:
            print(f"[paper-tracker] {sym}: no open price for {as_of} - skipped")
            continue
        prices[sym] = float(df.iloc[-1]["open"])
    return prices


def sync() -> None:
    """Advance the paper ledger to match the live script's current rebalance,
    fetching real fill prices via the OpenAlgo client. Idempotent: does
    nothing if the ledger already reflects the live state's rebalance_count.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import momentum_rank_strategy as live
    from openalgo import api

    state = _load_live_state()
    ledger = load_ledger()
    live_count = int(state.get("rebalance_count", 0))
    if live_count <= ledger["last_synced_rebalance_count"]:
        print(f"[paper-tracker] already synced through rebalance "
              f"#{ledger['last_synced_rebalance_count']} - nothing to do")
        return

    target = state["held"]
    prev_held = list(ledger["positions"].keys())
    sells, buys = live.diff_basket(prev_held, target)
    fill_date = date.today().strftime("%Y-%m-%d")

    client = api(api_key=live.API_KEY, host=live.HOST)
    needed = set(target) | set(prev_held)
    prices = _fetch_open_prices(client, sorted(needed), fill_date)
    missing = needed - prices.keys()
    if missing:
        raise RuntimeError(f"missing fill prices for {sorted(missing)} on "
                          f"{fill_date} - re-run once the market has opened")

    if not ledger["positions"] and not ledger["closed_trades"]:
        ledger = initial_buy(ledger, fill_date, target, prices)
        print(f"[paper-tracker] initial buy: {len(target)} names @ {fill_date} open")
    else:
        ledger = apply_rebalance(ledger, fill_date, sells, buys, target, prices)
        print(f"[paper-tracker] rebalance #{live_count}: "
              f"sold {sells or 'none'}, bought {buys or 'none'} @ {fill_date} open")
    ledger["last_synced_rebalance_count"] = live_count
    save_ledger(ledger)
    print_report(ledger, prices)


def print_report(ledger: dict | None = None,
                 current_prices: dict[str, float] | None = None) -> None:
    ledger = ledger or load_ledger()
    if current_prices is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import momentum_rank_strategy as live
        from openalgo import api
        client = api(api_key=live.API_KEY, host=live.HOST)
        today = date.today().strftime("%Y-%m-%d")
        current_prices = _fetch_open_prices(client, list(ledger["positions"]), today)

    s = summary(ledger, current_prices)
    print(f"MOMENTUM-RANK PAPER LEDGER - as of {datetime.now().date()}")
    print("=" * 44)
    print(f"Capital:        Rs {s['capital']:,.2f}")
    print(f"Total value:    Rs {s['total_value']:,.2f}")
    print(f"Total return:   {s['total_return_pct']:+.2f}%")
    print(f"Realized P&L:   Rs {s['realized_pnl']:,.2f}")
    print(f"Unrealized P&L: Rs {s['unrealized_pnl']:,.2f}")
    print(f"Cash:           Rs {s['cash']:,.2f}")
    print(f"Open positions: {s['open_positions']}  |  Closed trades: {s['closed_trades']}")
    print("-" * 44)
    for sym, pos in sorted(ledger["positions"].items()):
        cur = current_prices.get(sym, pos["entry_price"])
        pnl_pct = (cur - pos["entry_price"]) / pos["entry_price"] * 100
        print(f"  {sym:12s} qty={pos['qty']:>5} entry={pos['entry_price']:>9.2f} "
              f"cur={cur:>9.2f}  {pnl_pct:+.2f}%")
    if ledger["closed_trades"]:
        print("-" * 44)
        print("Closed trades:")
        for t in ledger["closed_trades"]:
            print(f"  {t['symbol']:12s} {t['entry_date']} -> {t['exit_date']}  "
                  f"{t['pnl_pct']:+.2f}%  (Rs {t['pnl']:+.2f})")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true",
                    help="advance the paper ledger to match the live rebalance state")
    ap.add_argument("--report", action="store_true", help="print current P&L")
    args = ap.parse_args()
    if args.sync:
        sync()
    elif args.report:
        print_report()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
