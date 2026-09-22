"""Reconciliation tests for signal_engine.analysis.

The ledger's whole job is to say what the broker actually did, so the cases that matter
are the ones where signal, order and fill DISAGREE. Each test below is one real failure
mode observed in the live audit trail or the Telegram export.
"""

import json
import sqlite3
from datetime import date, datetime

import pytest

from signal_engine.analysis.ledger import (
    FLAG_NO_ENTRY,
    FLAG_NO_FILL,
    FLAG_OPEN_AT_EOD,
    FLAG_QTY_MISMATCH,
    FLAG_UNMATCHED_FILL,
    build_ledger,
    load_engine_events,
    load_fills,
)

_COLS = ("strategy", "direction", "symbol", "entry", "sl", "tp", "quantity", "order_id",
         "status", "message", "signal_time", "received_at", "executed_at",
         "raw_message", "context", "fill_price")


def _make_db(tmp_path, rows):
    path = tmp_path / "trades.db"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE trades (id INTEGER PRIMARY KEY, {', '.join(_COLS)})")
    conn.executemany(
        f"INSERT INTO trades ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
        [tuple(r.get(c) for c in _COLS) for r in rows])
    conn.commit()
    conn.close()
    return str(path)


def _ev(direction, symbol="SBIN", entry=100.0, sl=98.0, tp=104.0, qty=10,
        oid="O1", status="SUCCESS", at="2026-08-28T09:50:00+05:30",
        reason=None, fill=None):
    return {"strategy": "ORB", "direction": direction, "symbol": symbol, "entry": entry,
            "sl": sl, "tp": tp, "quantity": qty, "order_id": oid, "status": status,
            "message": "ok", "signal_time": "", "received_at": at, "executed_at": at,
            "raw_message": "", "context": json.dumps({"reason": reason} if reason else {}),
            "fill_price": fill}


def _trade(oid, qty, price, symbol="SBIN", action="BUY"):
    return {"orderid": oid, "quantity": qty, "average_price": price,
            "symbol": symbol, "action": action, "timestamp": "09:50:01"}


# ---- sources --------------------------------------------------------------

def test_load_fills_aggregates_partial_fills_quantity_weighted():
    """One order can fill in several tranches; the ledger must not average them evenly."""
    fills = load_fills([_trade("O1", 10, 100.0), _trade("O1", 30, 110.0)])
    assert fills["O1"]["qty"] == 40
    assert fills["O1"]["price"] == pytest.approx(107.5)   # not 105


def test_load_fills_skips_rows_without_an_order_id():
    assert load_fills([{"quantity": 5, "average_price": 10.0}]) == {}


# ---- the happy path -------------------------------------------------------

def test_full_round_trip_with_partial_exits(tmp_path):
    """TP1 books half, TP1.5 the rest - the case a Telegram-only view gets wrong."""
    db = _make_db(tmp_path, [
        _ev("LONG", qty=10, oid="E1", at="2026-08-28T09:50:00+05:30"),
        _ev("EXIT", tp=104.0, qty=5, oid="X1", at="2026-08-28T10:30:00+05:30", reason="TP1"),
        _ev("EXIT", tp=106.0, qty=5, oid="X2", at="2026-08-28T11:00:00+05:30", reason="TP1.5"),
    ])
    fills = load_fills([_trade("E1", 10, 100.5), _trade("X1", 5, 103.8), _trade("X2", 5, 105.9)])
    pos = build_ledger(load_engine_events(db_path=db), fills)
    assert len(pos) == 1
    p = pos[0]
    assert p.flags == []
    assert p.entry_qty == 10 and p.exited_qty == 10 and p.residual_qty == 0
    assert p.avg_exit == pytest.approx(104.85)
    assert p.gross_pnl == pytest.approx((103.8 - 100.5) * 5 + (105.9 - 100.5) * 5)
    # risk is 2.00 per share off the ADVERTISED entry, over the 10 shares that round-tripped
    assert p.realised_r == pytest.approx(43.5 / (2.0 * 10))
    assert p.exit_reasons == "TP1+TP1.5"
    assert p.hold_minutes == pytest.approx(70.0)


def test_entry_slippage_is_signed_so_positive_always_means_it_cost_money(tmp_path):
    """A long filled above the alert price and a short filled below both lose."""
    long_db = _make_db(tmp_path, [_ev("LONG", entry=100.0, oid="E1")])
    p = build_ledger(load_engine_events(db_path=long_db), load_fills([_trade("E1", 10, 100.2)]))[0]
    assert p.entry_slippage_bps == pytest.approx(20.0)

    short_dir = tmp_path / "s"
    short_dir.mkdir()
    short_db = _make_db(short_dir, [_ev("SHORT", entry=100.0, sl=102.0, tp=96.0, oid="E2")])
    q = build_ledger(load_engine_events(db_path=short_db), load_fills([_trade("E2", 10, 99.8)]))[0]
    assert q.entry_slippage_bps == pytest.approx(20.0)


def test_stop_loss_legs_are_excluded_from_exit_slippage(tmp_path):
    """An SL-M fills wherever the market is; charging that as slippage hides the real number."""
    db = _make_db(tmp_path, [
        _ev("LONG", qty=10, oid="E1"),
        _ev("EXIT", tp=98.0, qty=10, oid="X1", at="2026-08-28T10:00:00+05:30", reason="SL"),
    ])
    p = build_ledger(load_engine_events(db_path=db),
                     load_fills([_trade("E1", 10, 100.0), _trade("X1", 10, 97.0)]))[0]
    assert p.exit_slippage_bps is None          # the only exit leg was an SL
    assert p.realised_r == pytest.approx(-1.5)  # but the loss is still counted in full


# ---- the failure modes this package exists to find ------------------------

def test_order_succeeded_but_broker_never_filled_it(tmp_path):
    db = _make_db(tmp_path, [_ev("LONG", oid="E1", status="SUCCESS")])
    p = build_ledger(load_engine_events(db_path=db), fills={})[0]
    assert FLAG_NO_FILL in p.flags
    assert p.realised_r is None


def test_position_left_open_at_end_of_session(tmp_path):
    """TP1 books half and nothing closes the rest - the live ORB residual-position bug."""
    db = _make_db(tmp_path, [
        _ev("LONG", qty=10, oid="E1"),
        _ev("EXIT", tp=104.0, qty=5, oid="X1", at="2026-08-28T10:30:00+05:30", reason="TP1"),
    ])
    p = build_ledger(load_engine_events(db_path=db),
                     load_fills([_trade("E1", 10, 100.0), _trade("X1", 5, 104.0)]))[0]
    assert FLAG_OPEN_AT_EOD in p.flags
    assert p.residual_qty == 5


def test_exit_ordered_but_never_filled_is_marked_in_the_exit_path(tmp_path):
    """An unfilled exit must be visible, not silently dropped from the summary."""
    db = _make_db(tmp_path, [
        _ev("LONG", qty=10, oid="E1"),
        _ev("EXIT", tp=104.0, qty=10, oid="X1", at="2026-08-28T10:30:00+05:30", reason="TP1"),
    ])
    p = build_ledger(load_engine_events(db_path=db), load_fills([_trade("E1", 10, 100.0)]))[0]
    assert FLAG_NO_FILL in p.flags
    assert p.exit_reasons == "TP1!"


def test_broker_filled_less_than_was_ordered(tmp_path):
    db = _make_db(tmp_path, [_ev("LONG", qty=100, oid="E1")])
    p = build_ledger(load_engine_events(db_path=db), load_fills([_trade("E1", 60, 100.0)]))[0]
    assert FLAG_QTY_MISMATCH in p.flags


def test_broker_fill_the_engine_never_sent(tmp_path):
    """Manual intervention or a broker auto-square-off. Must not vanish from the ledger."""
    db = _make_db(tmp_path, [_ev("LONG", oid="E1")])
    pos = build_ledger(load_engine_events(db_path=db),
                       load_fills([_trade("E1", 10, 100.0), _trade("ZZ9", 10, 99.0)]))
    assert any(FLAG_UNMATCHED_FILL in p.flags for p in pos)


def test_unmatched_fill_is_dated_from_the_fill_itself_not_date_min(tmp_path):
    """2026-09-22 EOD bug: `_unmatched_position` hardcoded `day=date.min`, so
    `p.day == target` (used by eod_review.py's check_trade_ledger and weekly_review.py's
    per-day buckets) could never match - a genuine same-day unmatched fill could never trip
    the regression check. Must carry the broker fill's own date instead.
    """
    db = _make_db(tmp_path, [_ev("LONG", oid="E1", at="2026-09-22T09:50:00+05:30")])
    fills = load_fills([
        _trade("E1", 10, 100.0),
        {**_trade("ZZ9", 5, 1026.8), "timestamp": "2026-09-22 10:25:07"},
    ])
    pos = build_ledger(load_engine_events(db_path=db), fills)
    unmatched = [p for p in pos if FLAG_UNMATCHED_FILL in p.flags]
    assert len(unmatched) == 1
    assert unmatched[0].day == date(2026, 9, 22)


def test_a_prior_days_already_reconciled_fill_does_not_pollute_a_later_days_review(tmp_path):
    """load_snapshots() pools every tradebook file ever captured, not just the day under
    review. Confirmed 2026-09-22: 13 fills from 2026-09-21's snapshot (already reconciled in
    that day's own report) showed up as "broker fills the engine never sent" in the
    2026-09-22 ledger, because 2026-09-21 wasn't in that day's engine events but was still
    in the pooled fills. Filtering positions to `p.day == target` (what the day-scoped
    callers already do) must exclude them once `day` is derived correctly.
    """
    db = _make_db(tmp_path, [_ev("LONG", oid="E1", at="2026-09-22T09:50:00+05:30")])
    fills = load_fills([
        _trade("E1", 10, 100.0),
        {**_trade("YESTERDAY", 13, 415.65), "timestamp": "2026-09-21 15:10:00"},
    ])
    pos = build_ledger(load_engine_events(db_path=db, since=date(2026, 9, 22)), fills)
    target = date(2026, 9, 22)
    unmatched_today = [p for p in pos if p.day == target and FLAG_UNMATCHED_FILL in p.flags]
    assert unmatched_today == []


def test_exit_without_a_preceding_entry(tmp_path):
    """What a mid-session restart that lost tracker state looks like."""
    db = _make_db(tmp_path, [_ev("EXIT", tp=104.0, oid="X1", reason="TP1")])
    p = build_ledger(load_engine_events(db_path=db), fills={})[0]
    assert FLAG_NO_ENTRY in p.flags


def test_persisted_fill_price_is_used_when_no_tradebook_snapshot_exists(tmp_path):
    """The broker tradebook is wiped daily; db.save's fill_price is the durable fallback."""
    db = _make_db(tmp_path, [
        _ev("LONG", entry=100.0, qty=10, oid="E1", fill=100.3),
        _ev("EXIT", tp=104.0, qty=10, oid="X1", at="2026-08-28T10:30:00+05:30",
            reason="TP1", fill=103.9),
    ])
    p = build_ledger(load_engine_events(db_path=db), fills={})[0]
    assert p.entry.filled and p.entry.fill_price == pytest.approx(100.3)
    assert p.entry_slippage_bps == pytest.approx(30.0)
    assert p.realised_r == pytest.approx((103.9 - 100.3) * 10 / (2.0 * 10))
    assert FLAG_QTY_MISMATCH not in p.flags   # quantity was assumed, so never flagged


def test_two_positions_in_the_same_symbol_on_the_same_day_do_not_merge(tmp_path):
    """The bug that corrupted the first pass over the Telegram export."""
    db = _make_db(tmp_path, [
        _ev("LONG", qty=10, oid="E1", at="2026-08-28T09:50:00+05:30"),
        _ev("EXIT", tp=104.0, qty=10, oid="X1", at="2026-08-28T10:00:00+05:30", reason="TP1"),
        _ev("LONG", qty=10, oid="E2", at="2026-08-28T11:00:00+05:30"),
        _ev("EXIT", tp=98.0, qty=10, oid="X2", at="2026-08-28T11:30:00+05:30", reason="SL"),
    ])
    pos = build_ledger(load_engine_events(db_path=db), fills={})
    assert len(pos) == 2
    assert [p.exit_reasons for p in pos] == ["TP1!", "SL!"]


def test_events_before_since_are_excluded(tmp_path):
    db = _make_db(tmp_path, [
        _ev("LONG", oid="E1", at="2026-08-01T09:50:00+05:30"),
        _ev("LONG", oid="E2", at="2026-08-28T09:50:00+05:30"),
    ])
    assert len(load_engine_events(db_path=db, since=datetime(2026, 8, 15).date())) == 1


def test_missing_database_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_engine_events(db_path=str(tmp_path / "nope.db"))
