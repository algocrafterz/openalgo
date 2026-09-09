"""A pending MARKET order must not sit open forever waiting on a dead quote feed.

On 2026-09-09 the Flattrade broker session went invalid for ~6 hours (session
expired before market open, no automatic re-login until a manual restart at
15:05 IST — see signal_engine/pinescripts/intraday/orb/breakout.md's postmortem
for that date). A HINDALCO MARKET order placed at 10:50 IST sat in "open"
status the whole time, then filled at 15:05 IST at whatever price happened to
be current then — four hours removed from the signal that placed it, after the
strategy had already told the trader TP1 and TP2 had been hit against it. That
produced a paper trade with no relationship to reality.

check_and_execute_pending_orders() now auto-cancels a MARKET order that has
been "open" for longer than max_market_order_age_seconds and still has no
quote to fill against, instead of leaving it to fill whenever the feed
recovers. LIMIT and SL/SL-M orders are exempt: resting for hours waiting on
their trigger price is their normal, intended behaviour, not a stuck order.
"""

import uuid
from datetime import datetime, timedelta

import pytest
import pytz

from database.sandbox_db import SandboxOrders, db_session
from sandbox.execution_engine import ExecutionEngine

IST = pytz.timezone("Asia/Kolkata")
USER_ID = "stale-order-test-user"


@pytest.fixture
def offline_engine(monkeypatch):
    """An engine with no quote source, so nothing fills - only the staleness
    guard can move an order out of "open"."""
    monkeypatch.setattr(ExecutionEngine, "_fetch_quotes_batch", lambda self, syms: {})
    monkeypatch.setattr(ExecutionEngine, "_check_pending_gtts", lambda self: None)
    engine = ExecutionEngine()
    engine.max_market_order_age_seconds = 60
    return engine


def _seed_order(*, price_type, age_seconds, order_status="open"):
    order_id = uuid.uuid4().hex[:14]
    placed_at = datetime.now(IST) - timedelta(seconds=age_seconds)
    db_session.add(
        SandboxOrders(
            orderid=order_id,
            user_id=USER_ID,
            symbol="RELIANCE",
            exchange="NSE",
            action="BUY",
            quantity=1,
            price=1,
            trigger_price=0,
            price_type=price_type,
            product="MIS",
            order_status=order_status,
            average_price=None,
            filled_quantity=0,
            pending_quantity=1,
            margin_blocked=0,
            strategy="stale-order-test",
            order_timestamp=placed_at,
            update_timestamp=placed_at,
        )
    )
    db_session.commit()
    return order_id


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    SandboxOrders.query.filter_by(strategy="stale-order-test").delete()
    db_session.commit()


def test_stale_market_order_is_cancelled(offline_engine):
    order_id = _seed_order(price_type="MARKET", age_seconds=600)

    offline_engine.check_and_execute_pending_orders()

    order = SandboxOrders.query.filter_by(orderid=order_id).first()
    assert order.order_status == "cancelled"
    assert order.rejection_reason
    assert "no valid quote" in order.rejection_reason.lower()


def test_fresh_market_order_is_left_pending(offline_engine):
    order_id = _seed_order(price_type="MARKET", age_seconds=5)

    offline_engine.check_and_execute_pending_orders()

    order = SandboxOrders.query.filter_by(orderid=order_id).first()
    assert order.order_status == "open"


def test_stale_limit_order_is_not_cancelled(offline_engine):
    """LIMIT orders legitimately rest for hours waiting on their price - the
    staleness guard must only ever apply to MARKET orders."""
    order_id = _seed_order(price_type="LIMIT", age_seconds=600)

    offline_engine.check_and_execute_pending_orders()

    order = SandboxOrders.query.filter_by(orderid=order_id).first()
    assert order.order_status == "open"


def test_stale_sl_m_trigger_pending_is_not_cancelled(offline_engine):
    """An SL/SL-M order resting in the exchange's Stop-Loss book (trigger
    pending) is also exempt - it hasn't even reached the regular order book
    yet, so "stuck without a fill" does not apply to it."""
    order_id = _seed_order(price_type="SL-M", age_seconds=600, order_status="trigger pending")

    offline_engine.check_and_execute_pending_orders()

    order = SandboxOrders.query.filter_by(orderid=order_id).first()
    assert order.order_status == "trigger pending"
