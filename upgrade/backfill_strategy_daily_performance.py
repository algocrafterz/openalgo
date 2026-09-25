#!/usr/bin/env python
"""
Backfill signal_engine EXIT trades into strategy_closed_trades.

Strategy Daily Performance (docs/strategy-daily-performance.md) originally
shipped forward-only: `database/strategy_book_db.py`'s StrategyClosedTrade
ledger only gets a row from the moment a live closing fill runs through
`_apply_fill_locked()`. That left every strategy's launch day empty, because
signal_engine strategies' exits close through a different path entirely -
`signal_engine/db.py`'s `save_tracker_exit()`, called from
`signal_engine/tracker.py` - which never touches strategy_book_db.

`save_tracker_exit()` DOES write an accurate per-trade record though: a row
in signal_engine/data/trades.db with `direction='EXIT'`, `fill_price` as the
exit price, `entry` as the original entry price, and the realized P&L inside
its `context` JSON column (`{"pnl": ..., "exit_types": [...]}`). This script
reads those rows and backfills strategy_closed_trades from them.

Scope: this ONLY recovers strategies that closed through signal_engine's
tracker. Flow and Python Strategy Host trades have no equivalent per-trade
exit log anywhere in the system to backfill from - see
docs/strategy-daily-performance.md's design decisions.

This is opt-in and manually run - deliberately NOT added to
upgrade/migrate_all.py's MIGRATIONS list, for the same reason
upgrade/rotate_pepper.py is absent from it: it is operator-controlled work,
not something to run unattended on every update.

Idempotent: re-running for the same date skips rows that already have a
matching StrategyClosedTrade (by user_id/strategy/symbol/mode/trade_date/
exit_price/realized_pnl).

Usage (run from the PROJECT ROOT, unlike most upgrade/ scripts - this one
reuses database/strategy_book_db.py's engine directly, which resolves its
SQLite path relative to the app's normal working directory):
    uv run python upgrade/backfill_strategy_daily_performance.py                  # today
    uv run python upgrade/backfill_strategy_daily_performance.py --date 2026-09-24
    uv run python upgrade/backfill_strategy_daily_performance.py --status --date 2026-09-25
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Register the app's SQLite pragmas on this process's engines - see _pragmas.py.
import _pragmas  # noqa: F401,E402

from utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNAL_ENGINE_TRADES_DB = os.path.join(_PROJECT_ROOT, "signal_engine", "data", "trades.db")


def _fetch_exit_rows(trade_date: str, db_path: str | None = None) -> list[dict]:
    """Every signal_engine EXIT row for a calendar date, oldest first.

    Reads SIGNAL_ENGINE_TRADES_DB from module scope at call time rather than
    as a default-parameter value - a default binds once at function
    definition, so a caller overriding the module constant (tests; a future
    multi-instance deployment) would otherwise silently keep hitting the
    original path.
    """
    db_path = db_path if db_path is not None else SIGNAL_ENGINE_TRADES_DB
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE direction='EXIT' AND date(executed_at)=? ORDER BY id",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _infer_direction(entry_price: float, exit_price: float, realized_pnl: float) -> str:
    """LONG pnl = qty*(exit-entry); SHORT pnl = qty*(entry-exit) - so the sign
    of realized_pnl relative to (exit-entry) alone tells us which it was,
    with no need to correlate back to the original entry row."""
    price_delta = exit_price - entry_price
    if price_delta == 0:
        return "LONG" if realized_pnl >= 0 else "SHORT"
    return "LONG" if (realized_pnl >= 0) == (price_delta >= 0) else "SHORT"


# 2026-09-25 incident: BREAKOUT/TCS backfilled at -578.00 from signal_engine's
# own context.pnl, but strategy_positions.realized_pnl (derived from OpenAlgo's
# actual booked order fills via StrategyOrderTag.applied_notional) was -821.60
# - a real ~30% understatement, traced to tracker.py's pnl_delta computation
# (it prefers the broker's own reported "realised" figure over a price-based
# calculation on the single-close path - see tracker.py check_positions,
# ~line 1020 - which has its own documented history of going stale/wrong).
# That is a signal_engine bug, not a backfill bug, and not something to paper
# over here. strategy_positions is the side verified against real fills (see
# docs/strategy-pnl-fork-modification.md's conclusion), so it wins whenever
# a leg is unambiguous: exactly one closed trade that day. RECONCILE_TOLERANCE
# absorbs float rounding, not genuine disagreement.
RECONCILE_TOLERANCE = 0.01


def _position_lookup(trade_date: str) -> dict:
    """(strategy, symbol, mode) -> {"exchange", "product", "realized_pnl"},
    sourced from strategy_positions rows touched the same day - the
    authoritative figure this script cross-checks signal_engine's
    self-reported P&L against, and the source for exchange/product too
    (signal_engine's trades table has no columns for either)."""
    from database.strategy_book_db import StrategyPosition, db_session

    lookup = {}
    try:
        rows = (
            db_session.query(StrategyPosition)
            .filter(StrategyPosition.trade_date == trade_date)
            .all()
        )
        for r in rows:
            lookup[(r.strategy, r.symbol, r.mode)] = {
                "exchange": r.exchange,
                "product": r.product,
                "realized_pnl": float(r.realized_pnl or 0.0),
            }
    except Exception:
        db_session.rollback()
        logger.exception("Could not build position lookup from strategy_positions")
    return lookup


def backfill(trade_date: str, dry_run: bool = False) -> tuple[int, int]:
    """Returns (inserted, skipped). skipped covers both "already present" and
    "EXIT row carried no pnl in its context" (defensive - save_tracker_exit
    always writes one, but a malformed/legacy row should not crash the run).

    Corrections and unreconciled multi-trade legs are logged via logger, not
    silently swallowed - see RECONCILE_TOLERANCE above for why."""
    from database.strategy_book_db import (
        StrategyClosedTrade,
        db_session,
        init_strategy_book_db,
    )

    init_strategy_book_db()
    exits = _fetch_exit_rows(trade_date)
    lookup = _position_lookup(trade_date)

    leg_counts: dict[tuple, int] = {}
    for row in exits:
        mode = (row.get("trade_mode") or "unknown").strip().lower() or "unknown"
        key = (row["strategy"], row["symbol"], mode)
        leg_counts[key] = leg_counts.get(key, 0) + 1

    inserted = skipped = corrected = 0
    for row in exits:
        try:
            context = json.loads(row.get("context") or "{}")
        except json.JSONDecodeError:
            context = {}
        pnl = context.get("pnl")
        if pnl is None:
            skipped += 1
            continue

        strategy = row["strategy"]
        symbol = row["symbol"]
        mode = (row.get("trade_mode") or "unknown").strip().lower() or "unknown"
        entry_price = float(row.get("entry") or 0.0)
        exit_price = float(row.get("fill_price") or 0.0)
        quantity = abs(float(row.get("quantity") or 0.0))
        realized_pnl = round(float(pnl), 4)

        leg_key = (strategy, symbol, mode)
        position = lookup.get(leg_key)
        if position is not None:
            verified_pnl = round(position["realized_pnl"], 4)
            diff = abs(verified_pnl - realized_pnl)
            if leg_counts[leg_key] == 1:
                # Unambiguous: this is the only trade that closed this leg
                # today, so any gap is signal_engine mis-stating its own
                # single number, not a multi-trade attribution problem.
                if diff > RECONCILE_TOLERANCE:
                    logger.warning(
                        f"Backfill correction {strategy}/{symbol} ({mode}, {trade_date}): "
                        f"signal_engine reported pnl={realized_pnl:,.2f} but "
                        f"strategy_positions.realized_pnl={verified_pnl:,.2f} (verified "
                        f"against actual fills) - using the verified figure"
                    )
                    realized_pnl = verified_pnl
                    corrected += 1
            elif diff > RECONCILE_TOLERANCE:
                # Multiple trades closed this leg today; the position-level
                # total cannot be safely split across them, so signal_engine's
                # individual figures are kept - but the mismatch is real and
                # worth a human's attention, not a silent divergence.
                logger.warning(
                    f"Unreconciled leg {strategy}/{symbol} ({mode}, {trade_date}): "
                    f"{leg_counts[leg_key]} trades closed today, signal_engine's sum "
                    f"disagrees with strategy_positions.realized_pnl={verified_pnl:,.2f} "
                    f"by {diff:,.2f} - per-trade figures kept as-is, review manually"
                )
        direction = _infer_direction(entry_price, exit_price, realized_pnl)
        exchange, product = (
            (position["exchange"], position["product"]) if position else ("NSE", "MIS")
        )
        # signal_engine places orders via API key, not a session - the live
        # write path (_apply_fill_locked) stores the same empty string for
        # these orders' StrategyOrderTag.user_id, so this matches it exactly.
        user_id = ""

        existing = (
            db_session.query(StrategyClosedTrade)
            .filter_by(
                user_id=user_id,
                strategy=strategy,
                symbol=symbol,
                mode=mode,
                trade_date=trade_date,
                exit_price=round(exit_price, 4),
                realized_pnl=realized_pnl,
            )
            .first()
        )
        if existing:
            skipped += 1
            continue

        if not dry_run:
            db_session.add(
                StrategyClosedTrade(
                    user_id=user_id,
                    strategy=strategy,
                    symbol=symbol,
                    exchange=exchange,
                    product=product,
                    mode=mode,
                    direction=direction,
                    closed_quantity=round(quantity, 4),
                    entry_price=round(entry_price, 4),
                    exit_price=round(exit_price, 4),
                    realized_pnl=realized_pnl,
                    trade_date=trade_date,
                )
            )
        inserted += 1

    if corrected:
        logger.info(f"Backfill for {trade_date}: corrected {corrected} leg(s) - see warnings above")

    if not dry_run and inserted:
        db_session.commit()
    return inserted, skipped


def status(trade_date: str) -> None:
    exits = _fetch_exit_rows(trade_date)
    inserted, skipped = backfill(trade_date, dry_run=True)
    print(f"signal_engine EXIT rows for {trade_date}: {len(exits)}")
    print(f"  would insert: {inserted}")
    print(f"  already present / unusable: {skipped}")


def reconcile(trade_date: str) -> bool:
    """Post-hoc check: does strategy_closed_trades already sum to
    strategy_positions.realized_pnl for every leg touched on trade_date?

    Independent of backfill() - this reads what is ALREADY persisted (from a
    prior backfill run, or from live _apply_fill_locked writes), so it also
    catches drift introduced after a backfill (e.g. a live trade booked
    between the backfill run and now). Intended for a periodic EOD check, not
    just a one-time post-backfill sanity pass - the same class of divergence
    this script's RECONCILE_TOLERANCE logic corrects for at insert time can
    reappear on the live path too, in principle, since it draws from a
    different signal_engine code path entirely (tracker.py's pnl_delta, not
    save_tracker_exit's context).

    Returns True iff every leg reconciles within RECONCILE_TOLERANCE.
    """
    from database.strategy_book_db import (
        StrategyClosedTrade,
        StrategyPosition,
        db_session,
        init_strategy_book_db,
    )

    init_strategy_book_db()
    positions = (
        db_session.query(StrategyPosition).filter(StrategyPosition.trade_date == trade_date).all()
    )
    closed = (
        db_session.query(StrategyClosedTrade)
        .filter(StrategyClosedTrade.trade_date == trade_date)
        .all()
    )
    closed_sum: dict[tuple, float] = {}
    for c in closed:
        key = (c.strategy, c.symbol, c.mode)
        closed_sum[key] = closed_sum.get(key, 0.0) + float(c.realized_pnl or 0)

    all_ok = True
    for p in positions:
        key = (p.strategy, p.symbol, p.mode)
        expected = float(p.realized_pnl or 0.0)
        actual = closed_sum.get(key, 0.0)
        diff = abs(expected - actual)
        if diff > RECONCILE_TOLERANCE:
            all_ok = False
            print(
                f"MISMATCH {p.strategy}/{p.symbol} ({p.mode}): "
                f"strategy_positions={expected:,.2f} strategy_closed_trades={actual:,.2f} "
                f"diff={diff:,.2f}"
            )
    if all_ok:
        print(
            f"Reconciled: every leg touched on {trade_date} matches within {RECONCILE_TOLERANCE}."
        )
    return all_ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill signal_engine trade exits into strategy_closed_trades.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, default: today (server date)")
    parser.add_argument("--status", action="store_true", help="Report what would happen, no writes")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Reconcile already-persisted strategy_closed_trades against strategy_positions, no writes",
    )
    args = parser.parse_args()

    target_date = args.date or datetime.now().date().isoformat()

    if args.verify:
        sys.exit(0 if reconcile(target_date) else 1)
    elif args.status:
        status(target_date)
    else:
        n_inserted, n_skipped = backfill(target_date)
        print(f"Backfilled {n_inserted} closed trade(s) for {target_date}, skipped {n_skipped}.")
