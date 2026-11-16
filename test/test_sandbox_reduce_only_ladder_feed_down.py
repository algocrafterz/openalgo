"""BUG-019: reduce-only MARKET closes with the feed down.

The auto square-off and the manual close send untagged orders: they keep the REST quote path
and fill even after hours / with the feed down. Only strategy-tagged reduce-only exits use the
no-REST ladder (accepted open at the last position price until a tick arrives).
"""

from __future__ import annotations

import pytest
from test_gthread_sandbox_squareoff import (
    USER,
    _due_manager,
    _seed_mis_long,
    _state,
    fresh_account,  # noqa: F401  (autouse fixture)
)
from test_gthread_sandbox_support import quote, release_sessions, run_in_thread


def _feed_down_with_rest(monkeypatch):
    """Feed down, REST quote available: an untagged order must take the REST path."""
    from sandbox.execution_engine import ExecutionEngine

    rest = []

    def fetch(self, symbol, exchange):
        rest.append(symbol)
        return quote(2500)

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", fetch)
    monkeypatch.setattr("sandbox.feed_price.feed_quote", lambda s, e, a: None)
    return rest


def _orders():
    from database.sandbox_db import SandboxOrders, db_session

    db_session.remove()
    rows = SandboxOrders.query.filter_by(user_id=USER, symbol="RELIANCE").all()
    out = [(o.action, o.order_status, o.quantity) for o in rows]
    db_session.remove()
    return out


def test_auto_square_off_with_the_feed_down_takes_the_rest_quote_path(monkeypatch):
    _seed_mis_long()
    rest = _feed_down_with_rest(monkeypatch)
    manager = _due_manager()
    run_in_thread(manager.check_and_square_off)
    assert rest, "an untagged square-off must use the REST quote path"
    assert _orders() == [("SELL", "complete", 50)]
    assert _state()["quantity"] == 0


def test_after_hours_close_takes_the_rest_quote_path(monkeypatch):
    from sandbox.position_manager import PositionManager

    _seed_mis_long()
    rest = _feed_down_with_rest(monkeypatch)
    ok, response, _ = run_in_thread(
        lambda: PositionManager(USER).close_position("RELIANCE", "BSE", "MIS")
    )
    release_sessions()
    assert rest, "an untagged close must use the REST quote path"
    assert _orders() == [("SELL", "complete", 50)], (ok, response)


def test_strategy_tagged_exit_with_the_feed_down_is_accepted_open_with_no_rest(monkeypatch):
    from sandbox.order_manager import OrderManager

    _seed_mis_long()
    rest = _feed_down_with_rest(monkeypatch)
    order = {
        "symbol": "RELIANCE",
        "exchange": "BSE",
        "action": "SELL",
        "quantity": 50,
        "price_type": "MARKET",
        "product": "MIS",
        "strategy": "EVWAP-EARLY-PY",
    }
    run_in_thread(lambda: OrderManager(USER).place_order(order))
    release_sessions()
    assert rest == [], "a tagged reduce-only exit must never call the REST quote function"
    assert _orders() == [("SELL", "open", 50)]
    assert _state()["quantity"] == 50
