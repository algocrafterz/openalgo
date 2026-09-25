"""
Unit tests for upgrade/backfill_strategy_daily_performance.py.

Run with: uv run pytest test/test_backfill_strategy_daily_performance.py -v
"""

import json
import os
import sqlite3
import sys
import uuid

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "upgrade")
)

import backfill_strategy_daily_performance as backfill_module  # noqa: E402

from database.strategy_book_db import (  # noqa: E402
    StrategyClosedTrade,
    StrategyPosition,
    db_session,
    init_strategy_book_db,
)


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


@pytest.fixture()
def signal_engine_db(tmp_path, monkeypatch):
    """A throwaway signal_engine-shaped trades.db, wired in as the module's
    source for the duration of the test."""
    db_path = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT, direction TEXT, symbol TEXT, entry REAL, sl REAL, tp REAL,
            quantity INTEGER, order_id TEXT, status TEXT, message TEXT,
            signal_time TEXT, received_at TEXT, executed_at TEXT,
            raw_message TEXT, context TEXT, fill_price REAL, sig_id TEXT, trade_mode TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(backfill_module, "SIGNAL_ENGINE_TRADES_DB", db_path)
    return db_path


def _insert_exit(
    db_path, *, strategy, symbol, entry, fill_price, pnl, executed_at, trade_mode="live"
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO trades (strategy, direction, symbol, entry, sl, tp, quantity, order_id,
                             status, message, signal_time, received_at, executed_at,
                             raw_message, context, fill_price, sig_id, trade_mode)
        VALUES (?, 'EXIT', ?, ?, 0, 0, ?, 'oid', 'SUCCESS', 'msg', '', ?, ?, '', ?, ?, 'sig', ?)
        """,
        (
            strategy,
            symbol,
            entry,
            10,
            executed_at,
            executed_at,
            json.dumps({"pnl": pnl, "exit_types": ["SL"]}),
            fill_price,
            trade_mode,
        ),
    )
    conn.commit()
    conn.close()


def test_infer_direction_long_profit():
    # LONG closed at a higher price than entry, profit -> LONG
    assert (
        backfill_module._infer_direction(entry_price=100.0, exit_price=110.0, realized_pnl=100.0)
        == "LONG"
    )


def test_infer_direction_long_loss():
    # LONG closed lower than entry, loss -> still LONG (both pnl and delta negative)
    assert (
        backfill_module._infer_direction(entry_price=100.0, exit_price=90.0, realized_pnl=-100.0)
        == "LONG"
    )


def test_infer_direction_short_profit():
    # SHORT closed lower than entry, profit -> SHORT (pnl positive, price delta negative)
    assert (
        backfill_module._infer_direction(entry_price=100.0, exit_price=90.0, realized_pnl=100.0)
        == "SHORT"
    )


def test_infer_direction_short_loss():
    assert (
        backfill_module._infer_direction(entry_price=100.0, exit_price=110.0, realized_pnl=-100.0)
        == "SHORT"
    )


def test_backfill_inserts_closed_trades_from_signal_engine_db(signal_engine_db):
    strategy = f"BACKFILL-{uuid.uuid4().hex[:8]}"
    trade_date = "2026-08-01"
    _insert_exit(
        signal_engine_db,
        strategy=strategy,
        symbol="SBIN",
        entry=100.0,
        fill_price=110.0,
        pnl=100.0,
        executed_at=f"{trade_date}T10:00:00",
        trade_mode="live",
    )

    inserted, skipped = backfill_module.backfill(trade_date)

    assert inserted == 1
    assert skipped == 0

    rows = (
        db_session.query(StrategyClosedTrade)
        .filter_by(strategy=strategy, mode="live", trade_date=trade_date)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].realized_pnl == pytest.approx(100.0)
    assert rows[0].direction == "LONG"
    assert rows[0].exchange == "NSE"  # no strategy_positions row that day -> falls back to default
    assert rows[0].product == "MIS"


def test_backfill_is_idempotent_on_rerun(signal_engine_db):
    strategy = f"BACKFILL-IDEMP-{uuid.uuid4().hex[:8]}"
    trade_date = "2026-08-02"
    _insert_exit(
        signal_engine_db,
        strategy=strategy,
        symbol="TCS",
        entry=200.0,
        fill_price=190.0,
        pnl=-100.0,
        executed_at=f"{trade_date}T11:00:00",
    )

    first_inserted, first_skipped = backfill_module.backfill(trade_date)
    second_inserted, second_skipped = backfill_module.backfill(trade_date)

    assert (first_inserted, first_skipped) == (1, 0)
    assert (second_inserted, second_skipped) == (0, 1)


def test_backfill_skips_exit_rows_with_no_pnl_in_context(signal_engine_db):
    conn = sqlite3.connect(signal_engine_db)
    conn.execute(
        """
        INSERT INTO trades (strategy, direction, symbol, entry, fill_price, executed_at, context, trade_mode)
        VALUES ('NOPNL', 'EXIT', 'INFY', 100, 105, '2026-08-03T10:00:00', '{}', 'live')
        """
    )
    conn.commit()
    conn.close()

    inserted, skipped = backfill_module.backfill("2026-08-03")

    assert inserted == 0
    assert skipped == 1


def test_backfill_returns_zero_when_source_db_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        backfill_module, "SIGNAL_ENGINE_TRADES_DB", str(tmp_path / "does-not-exist.db")
    )

    inserted, skipped = backfill_module.backfill("2026-08-04")

    assert (inserted, skipped) == (0, 0)


def _add_position(
    *, strategy, symbol, mode, trade_date, realized_pnl, exchange="NSE", product="MIS"
):
    """Directly inserts a StrategyPosition row - this reconciliation logic
    only reads realized_pnl/exchange/product, so there's no need to replay a
    fill sequence through apply_fill() to get one."""
    db_session.add(
        StrategyPosition(
            user_id="",
            strategy=strategy,
            symbol=symbol,
            exchange=exchange,
            product=product,
            mode=mode,
            quantity=0.0,
            average_price=0.0,
            realized_pnl=realized_pnl,
            today_realized_pnl=realized_pnl,
            trade_date=trade_date,
        )
    )
    db_session.commit()


def test_backfill_corrects_realized_pnl_when_single_trade_disagrees_with_strategy_positions(
    signal_engine_db,
):
    """The 2026-09-25 BREAKOUT/TCS incident this regression-tests: signal_engine
    self-reported pnl=-578.00 for a leg's only close that day, while
    strategy_positions.realized_pnl (derived from OpenAlgo's actual booked
    fills) was -821.60. The verified figure must win."""
    strategy = f"CORRECT-{uuid.uuid4().hex[:8]}"
    trade_date = "2026-08-05"
    _add_position(
        strategy=strategy, symbol="TCS", mode="live", trade_date=trade_date, realized_pnl=-821.60
    )
    _insert_exit(
        signal_engine_db,
        strategy=strategy,
        symbol="TCS",
        entry=2076.8,
        fill_price=2082.53,
        pnl=-578.00,
        executed_at=f"{trade_date}T10:45:00",
        trade_mode="live",
    )

    inserted, skipped = backfill_module.backfill(trade_date)

    assert (inserted, skipped) == (1, 0)
    row = (
        db_session.query(StrategyClosedTrade)
        .filter_by(strategy=strategy, symbol="TCS", mode="live", trade_date=trade_date)
        .one()
    )
    assert row.realized_pnl == pytest.approx(-821.60)  # corrected, not -578.00


def test_backfill_leaves_multi_trade_leg_uncorrected(signal_engine_db):
    """Two closes on the same leg in one day: the position-level total cannot
    be safely split between them, so each trade keeps its own signal_engine
    figure even if the leg-level sum disagrees with strategy_positions."""
    strategy = f"MULTI-{uuid.uuid4().hex[:8]}"
    trade_date = "2026-08-06"
    _add_position(
        strategy=strategy, symbol="INFY", mode="live", trade_date=trade_date, realized_pnl=-999.0
    )
    _insert_exit(
        signal_engine_db,
        strategy=strategy,
        symbol="INFY",
        entry=100.0,
        fill_price=90.0,
        pnl=-100.0,
        executed_at=f"{trade_date}T10:00:00",
        trade_mode="live",
    )
    _insert_exit(
        signal_engine_db,
        strategy=strategy,
        symbol="INFY",
        entry=100.0,
        fill_price=95.0,
        pnl=-50.0,
        executed_at=f"{trade_date}T11:00:00",
        trade_mode="live",
    )

    inserted, skipped = backfill_module.backfill(trade_date)

    assert (inserted, skipped) == (2, 0)
    rows = (
        db_session.query(StrategyClosedTrade)
        .filter_by(strategy=strategy, symbol="INFY", mode="live", trade_date=trade_date)
        .all()
    )
    assert sorted(r.realized_pnl for r in rows) == [-100.0, -50.0]  # untouched


def test_reconcile_flags_mismatch_between_positions_and_closed_trades():
    strategy = f"RECON-{uuid.uuid4().hex[:8]}"
    trade_date = "2026-08-07"
    _add_position(
        strategy=strategy, symbol="WIPRO", mode="live", trade_date=trade_date, realized_pnl=-500.0
    )
    db_session.add(
        StrategyClosedTrade(
            user_id="",
            strategy=strategy,
            symbol="WIPRO",
            exchange="NSE",
            product="MIS",
            mode="live",
            direction="LONG",
            closed_quantity=10,
            entry_price=100,
            exit_price=95,
            realized_pnl=-300.0,  # deliberately does not match the position's -500.0
            trade_date=trade_date,
        )
    )
    db_session.commit()

    assert backfill_module.reconcile(trade_date) is False


def test_reconcile_passes_when_totals_match():
    strategy = f"RECONOK-{uuid.uuid4().hex[:8]}"
    trade_date = "2026-08-08"
    _add_position(
        strategy=strategy, symbol="HCLTECH", mode="live", trade_date=trade_date, realized_pnl=200.0
    )
    db_session.add(
        StrategyClosedTrade(
            user_id="",
            strategy=strategy,
            symbol="HCLTECH",
            exchange="NSE",
            product="MIS",
            mode="live",
            direction="LONG",
            closed_quantity=10,
            entry_price=100,
            exit_price=120,
            realized_pnl=200.0,
            trade_date=trade_date,
        )
    )
    db_session.commit()

    assert backfill_module.reconcile(trade_date) is True
