"""trades.db is the source of truth, so its hot queries must not be full scans.

The table carried NO indexes at all. That was survivable while it was read twice a day at
startup; it is not now that fetch_day_trades() backs both the EOD summary AND the day-context
line on every close notification, and the table grows forever (the audit trail is deliberately
never pruned).

Three queries matter:
  fetch_day_trades        - (trade_mode, day, status, direction)
  fetch_last_entry_trade  - (symbol, strategy, day), case-insensitive
  fetch_all_open_positions- (status, direction, day)

The symbol/strategy lookup compares with upper(), so it needs an EXPRESSION index - a plain
column index cannot serve upper(symbol).
"""

import pytest

from signal_engine import db
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    db.set_trade_mode("analyze")
    yield
    db.reset_connection()


def _plan(sql, params):
    rows = db._get_connection().execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " ".join(str(r[-1]) for r in rows)


class TestHotQueriesUseAnIndex:
    def test_the_day_query_is_not_a_full_scan(self):
        plan = _plan(
            "SELECT 1 FROM trades WHERE status='SUCCESS' AND direction='EXIT' "
            "AND trade_mode=? AND date(executed_at)=?", ("analyze", "2026-09-11"))
        assert "SCAN trades" not in plan, plan

    def test_the_entry_lookup_is_not_a_full_scan(self):
        plan = _plan(
            "SELECT 1 FROM trades WHERE upper(symbol)=upper(?) AND upper(strategy)=upper(?) "
            "AND status='SUCCESS' AND direction IN ('LONG','SHORT') AND date(executed_at)=?",
            ("SBIN", "ORB", "2026-09-11"))
        assert "SCAN trades" not in plan, plan

    def test_indexes_are_created_on_a_fresh_database(self):
        names = {r[0] for r in db._get_connection().execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'")}
        assert "idx_trades_day" in names
        assert "idx_trades_symbol_lookup" in names

    def test_creating_them_twice_is_harmless(self):
        db.reset_connection()
        db._get_connection()  # must not raise


class TestTheDayQueryDoesNotRunOnePerRow:
    def test_entries_are_fetched_in_one_query_not_per_exit(self):
        for i in range(6):
            sym = f"SYM{i}"
            db.save(
                Signal(strategy="ORB", direction=Direction.LONG, symbol=sym,
                       entry=800.0, sl=796.0, tp=810.0, raw_message="x"),
                Order(symbol=sym, exchange="NSE", action=Action.BUY, quantity=10, price=0.0,
                      order_type="MARKET", product="MIS", strategy_tag="ORB"),
                TradeResult(status=OrderStatus.SUCCESS, order_id=f"E{i}"))
            db.save_tracker_exit(strategy="ORB", symbol=sym, entry=800.0, sl=796.0, tp=810.0,
                                 quantity=10, exit_price=805.0, pnl=10.0, exit_types=["TP1"])

        conn = db._get_connection()
        seen = []
        conn.set_trace_callback(seen.append)  # sqlite3's own hook; execute() is read-only
        try:
            rows = db.fetch_day_trades("analyze")
        finally:
            conn.set_trace_callback(None)

        assert len(rows) == 6
        selects = [q for q in seen if "SELECT" in q.upper()]
        assert len(selects) <= 2, f"{len(selects)} SELECTs for 6 exits - N+1"

    def test_each_trade_still_gets_its_own_entry_stop(self):
        for sym, sl in (("AAA", 790.0), ("BBB", 780.0)):
            db.save(
                Signal(strategy="ORB", direction=Direction.LONG, symbol=sym,
                       entry=800.0, sl=sl, tp=810.0, raw_message="x"),
                Order(symbol=sym, exchange="NSE", action=Action.BUY, quantity=10, price=0.0,
                      order_type="MARKET", product="MIS", strategy_tag="ORB"),
                TradeResult(status=OrderStatus.SUCCESS, order_id=f"E-{sym}"))
            db.save_tracker_exit(strategy="ORB", symbol=sym, entry=800.0, sl=sl, tp=810.0,
                                 quantity=10, exit_price=805.0, pnl=50.0, exit_types=["TP1"])
        by_symbol = {r["symbol"]: r["sl"] for r in db.fetch_day_trades("analyze")}
        assert by_symbol == {"AAA": 790.0, "BBB": 780.0}
