"""
Unit tests: audit_ledger_consistency() flags any flat strategy leg whose
realized_pnl does not match the sum of its own StrategyClosedTrade rows.

Added 2026-09-28 alongside LEDGER_RELIABLE_SINCE (services/strategy_metrics_
service.py) and the historical backfill of the "Open qty"/missing-trades bug
- this is the ongoing safeguard so a recurrence of that defect class surfaces
in log/errors.jsonl on the next restart instead of staying silent for weeks.

Run with: uv run pytest test/test_audit_ledger_consistency.py -v
"""

import uuid

import pytest

from database.strategy_book_db import (
    StrategyClosedTrade,
    apply_fill,
    audit_ledger_consistency,
    close_all_legs_for_position,
    db_session,
    init_strategy_book_db,
    record_order_tag,
)


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


def _oid():
    return f"test-audit-ledger-{uuid.uuid4().hex}"


def _open_and_close_leg(strategy, symbol, exchange, product, mode, qty, entry, exit_price):
    """A normal, fully-tagged open+close - should always leave a consistent
    ledger, since apply_fill writes the StrategyClosedTrade row itself."""
    open_oid, close_oid = _oid(), _oid()
    for oid in (open_oid, close_oid):
        record_order_tag(
            orderid=oid,
            user_id="",
            strategy=strategy,
            symbol=symbol,
            exchange=exchange,
            product=product,
            mode=mode,
        )
    apply_fill(open_oid, filled_quantity=qty, average_price=entry, action="BUY")
    apply_fill(close_oid, filled_quantity=qty, average_price=exit_price, action="SELL")


def test_a_normally_closed_leg_is_never_flagged():
    strategy = f"AUDIT-CLEAN-{uuid.uuid4().hex[:8]}"
    symbol = "AUDCLEAN" + uuid.uuid4().hex[:6].upper()
    _open_and_close_leg(strategy, symbol, "NSE", "MIS", "analyze", 10, 100.0, 110.0)

    mismatches = audit_ledger_consistency()
    assert not any(m["strategy"] == strategy for m in mismatches)


def test_a_leg_closed_via_close_all_legs_for_position_is_never_flagged():
    strategy = f"AUDIT-RECONCILE-{uuid.uuid4().hex[:8]}"
    symbol = "AUDRECON" + uuid.uuid4().hex[:6].upper()
    oid = _oid()
    record_order_tag(
        orderid=oid,
        user_id="",
        strategy=strategy,
        symbol=symbol,
        exchange="NSE",
        product="MIS",
        mode="analyze",
    )
    apply_fill(oid, filled_quantity=10, average_price=100.0, action="BUY")
    close_all_legs_for_position(
        symbol=symbol, exchange="NSE", product="MIS", mode="analyze", exit_price=105.0
    )

    mismatches = audit_ledger_consistency()
    assert not any(m["strategy"] == strategy for m in mismatches)


def test_a_leg_with_realized_pnl_but_no_ledger_row_is_flagged():
    """Simulates the exact defect class this audit exists to catch: a close
    that updated the running realized_pnl total without writing its
    StrategyClosedTrade row (directly manipulating the row the way the old,
    now-fixed bug effectively did - not through the public API, since the
    public API no longer allows this to happen)."""
    strategy = f"AUDIT-GAP-{uuid.uuid4().hex[:8]}"
    symbol = "AUDGAP" + uuid.uuid4().hex[:6].upper()
    _open_and_close_leg(strategy, symbol, "NSE", "MIS", "analyze", 10, 100.0, 110.0)

    # Delete the ledger row the close should have written, simulating the bug.
    db_session.query(StrategyClosedTrade).filter_by(strategy=strategy, symbol=symbol).delete()
    db_session.commit()

    mismatches = audit_ledger_consistency()
    matches = [m for m in mismatches if m["strategy"] == strategy]
    assert len(matches) == 1
    assert matches[0]["ledger_sum"] == 0.0
    assert matches[0]["leg_realized_pnl"] == pytest.approx(100.0)


def test_legacy_unknown_mode_is_excluded_by_default_but_included_on_request():
    strategy = f"AUDIT-LEGACY-{uuid.uuid4().hex[:8]}"
    symbol = "AUDLEGACY" + uuid.uuid4().hex[:6].upper()
    _open_and_close_leg(strategy, symbol, "NSE", "MIS", "unknown", 10, 100.0, 110.0)
    db_session.query(StrategyClosedTrade).filter_by(strategy=strategy, symbol=symbol).delete()
    db_session.commit()

    default_scope = audit_ledger_consistency()
    assert not any(m["strategy"] == strategy for m in default_scope)

    full_scope = audit_ledger_consistency(modes=())
    assert any(m["strategy"] == strategy for m in full_scope)
