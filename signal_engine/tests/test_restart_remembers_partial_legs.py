"""R-01 + restart: partial legs already booked to the risk counters are remembered on restore,
so the closing leg is priced on its own and the legs are not counted a second time."""

from types import SimpleNamespace
from unittest.mock import patch

from signal_engine import db, startup
from signal_engine.models import Action, Direction, Order, OrderStatus, TradeResult
from signal_engine.tests.conftest import make_signal
from signal_engine.tests.tracker_fixtures import _make_engine
from signal_engine.tracker import PositionTracker


def _order(action):
    return Order(
        symbol="SBIN", exchange="NSE", action=action, quantity=10, price=0.0,
        order_type="MARKET", product="MIS", strategy_tag="BREAKOUT",
    )


def _ok(oid):
    return TradeResult(order_id=oid, status=OrderStatus.SUCCESS, message="success")


def _entry():
    db.save(make_signal(strategy="BREAKOUT", symbol="SBIN"), _order(Action.BUY), _ok("E1"))


def _leg(pnl, partial=True):
    sig = make_signal(strategy="BREAKOUT", symbol="SBIN", direction=Direction.EXIT)
    sig.context = {"pnl": pnl, "exit_types": ["TP1"], "partial": partial}
    db.save(sig, _order(Action.SELL), _ok("X1"))


def test_no_legs_is_zero():
    _entry()
    assert db.fetch_open_position_leg_pnl("SBIN", "BREAKOUT") == 0.0


def test_partial_legs_are_summed():
    _entry()
    _leg(500.0)
    _leg(120.0)
    assert db.fetch_open_position_leg_pnl("SBIN", "BREAKOUT") == 620.0


def test_legs_of_an_earlier_position_are_not_counted():
    _entry()
    _leg(500.0)
    _entry()  # a new position later the same day
    assert db.fetch_open_position_leg_pnl("SBIN", "BREAKOUT") == 0.0


def test_restore_sets_realized_pnl_from_the_legs():
    _entry()
    _leg(500.0)
    tracker = PositionTracker(_make_engine())
    cfg = SimpleNamespace(
        product="MIS", exchange="NSE", strategy_profiles={"BREAKOUT": {}}
    )
    broker = [{"symbol": "SBIN", "exchange": "NSE", "quantity": 5, "average_price": 2500.0}]
    with patch.object(startup, "settings", cfg):
        startup._restore_tracker_positions(tracker, broker, "MIS")
    pos = tracker.find_position("SBIN", "BREAKOUT")
    assert pos is not None and pos.realized_pnl == 500.0


def test_restore_keeps_the_real_entry_time():
    from datetime import datetime, timedelta

    from signal_engine.timeutils import IST

    when = (datetime.now(IST) - timedelta(hours=3)).replace(tzinfo=None, microsecond=0)
    result = TradeResult(order_id="E1", status=OrderStatus.SUCCESS, message="success", timestamp=when)
    db.save(make_signal(strategy="BREAKOUT", symbol="SBIN"), _order(Action.BUY), result)
    tracker = PositionTracker(_make_engine())
    cfg = SimpleNamespace(product="MIS", exchange="NSE", strategy_profiles={"BREAKOUT": {}})
    broker = [{"symbol": "SBIN", "exchange": "NSE", "quantity": 10, "average_price": 2500.0}]
    with patch.object(startup, "settings", cfg):
        startup._restore_tracker_positions(tracker, broker, "MIS")
    pos = tracker.find_position("SBIN", "BREAKOUT")
    assert pos.entry_time == when.replace(tzinfo=IST)
