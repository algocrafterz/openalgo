"""BUG-019: a reduce-only MARKET close with the feed down is accepted open, never refused.

The auto square-off and the manual close send untagged reduce-only MARKET orders. With no
fresh feed tick (feed down, or after hours) they are accepted open at the position's last
known price, with no REST quote call, and filled by the execution engine on the next tick.
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
from test_gthread_sandbox_support import release_sessions, run_in_thread


def _feed_down_and_no_rest(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    rest = []

    def fetch(self, symbol, exchange):
        rest.append(symbol)
        raise AssertionError("a reduce-only MARKET order must not look up a live quote")

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


def test_auto_square_off_with_the_feed_down_accepts_an_open_order_instead_of_failing(monkeypatch):
    _seed_mis_long()
    rest = _feed_down_and_no_rest(monkeypatch)
    manager = _due_manager()
    run_in_thread(manager.check_and_square_off)
    assert rest == []
    assert _orders() == [("SELL", "open", 50)]
    assert _state()["quantity"] == 50  # still held: the engine fills on the next tick


def test_after_hours_close_is_accepted_open(monkeypatch):
    from sandbox.position_manager import PositionManager

    _seed_mis_long()
    rest = _feed_down_and_no_rest(monkeypatch)
    ok, response, _ = run_in_thread(
        lambda: PositionManager(USER).close_position("RELIANCE", "BSE", "MIS")
    )
    release_sessions()
    assert rest == []
    assert _orders() == [("SELL", "open", 50)], (ok, response)
