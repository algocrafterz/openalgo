"""Every trade row must say which mode produced it.

risk.db already keys its counters on (mode, date) — the isolation exists there because mixing
paper losses into live totals would be wrong. trades.db, the actual audit trail the ledger and
every performance report read, had no such column.

From 2026-09-07 that stops being theoretical: a week of ANALYZE-mode paper trades lands in the
same table as the 221 real ORB trades from March-August, and the only thing separating them is
the date — which fails the moment a strategy is paper-tested while another runs live, or the
mode is switched mid-session.
"""

import sqlite3

import pytest

from signal_engine.db import (
    DECLINED_ORDER_ID,
    UNKNOWN_TRADE_MODE,
    save,
    save_declined,
    set_trade_mode,
)
from signal_engine.models import Direction, Order, OrderStatus, Signal, TradeResult


@pytest.fixture(autouse=True)
def _reset_mode():
    yield
    set_trade_mode(UNKNOWN_TRADE_MODE)


def _sig():
    return Signal(
        strategy="BREAKOUT", direction=Direction.LONG, symbol="LICHSGFIN",
        entry=564.2, sl=562.61, tp=566.59, sig_id="LICHSGFIN-20260904-1045",
        raw_message="x",
    )


def _order():
    return Order(symbol="LICHSGFIN", exchange="NSE", action="BUY", quantity=10,
                 price=564.2, order_type="MARKET", product="MIS", strategy_tag="BREAKOUT")


def _rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM trades")]
    finally:
        conn.close()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "trades.db"
    monkeypatch.setattr("signal_engine.db._DB_PATH", str(path))
    return path


class TestTradeModeIsRecorded:
    def test_a_saved_trade_carries_the_active_mode(self, db):
        set_trade_mode("analyze")
        save(_sig(), _order(), TradeResult(order_id="X1", status=OrderStatus.SUCCESS, message="ok"))
        assert _rows(db)[0]["trade_mode"] == "analyze"

    def test_a_declined_signal_carries_it_too(self, db):
        """A decline is mode-specific: the analyze profile allows 6 slots, live allows 2, so
        'declined for a full slot' means different things in each and must not be pooled."""
        set_trade_mode("analyze")
        save_declined(_sig(), stage="risk_gates", reason="slot full")
        row = _rows(db)[0]
        assert row["trade_mode"] == "analyze"
        assert row["order_id"] == DECLINED_ORDER_ID

    def test_live_mode_is_recorded_as_live(self, db):
        set_trade_mode("live")
        save(_sig(), _order(), TradeResult(order_id="X2", status=OrderStatus.SUCCESS, message="ok"))
        assert _rows(db)[0]["trade_mode"] == "live"

    def test_mode_is_unknown_until_startup_sets_it(self, db):
        """The engine builds its objects at import, before OpenAlgo can be asked anything.
        A row written in that window must say 'unknown', never guess 'live'."""
        save(_sig(), _order(), TradeResult(order_id="X3", status=OrderStatus.SUCCESS, message="ok"))
        assert _rows(db)[0]["trade_mode"] == UNKNOWN_TRADE_MODE


class TestExistingDatabaseMigrates:
    def test_column_is_added_to_a_pre_existing_table_without_it(self, db):
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, "
            "direction TEXT, symbol TEXT, entry REAL, sl REAL, tp REAL, quantity INTEGER, "
            "order_id TEXT, status TEXT, message TEXT, signal_time TEXT, received_at TEXT, "
            "executed_at TEXT)"
        )
        conn.execute("INSERT INTO trades (strategy, symbol) VALUES ('ORB', 'SBIN')")
        conn.commit()
        conn.close()

        set_trade_mode("analyze")
        save(_sig(), _order(), TradeResult(order_id="X4", status=OrderStatus.SUCCESS, message="ok"))

        rows = _rows(db)
        assert len(rows) == 2
        legacy = next(r for r in rows if r["symbol"] == "SBIN")
        fresh = next(r for r in rows if r["symbol"] == "LICHSGFIN")
        assert fresh["trade_mode"] == "analyze"
        # The pre-existing row is NOT asserted to be live. The engine has an analyze mode and
        # an off-hours testing switch, so some historical rows may not be live trades and
        # nothing in the data distinguishes them. Backfilling a guess would be worse than a
        # null that analysis can see and exclude.
        assert legacy["trade_mode"] is None
