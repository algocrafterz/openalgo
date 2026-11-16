"""R-01 + restart: partial legs already booked to the risk counters are remembered on restore,
so the closing leg is priced on its own and the legs are not counted a second time."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


def _freeze_db_clock(monkeypatch, hh, mm, ss=0):
    """Pin db.datetime.now(IST) so 'today' (the trades.db date filter) does not depend on the wall clock."""
    from datetime import datetime

    from signal_engine.timeutils import IST

    frozen = datetime.now(IST).replace(hour=hh, minute=mm, second=ss, microsecond=0)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(db, "datetime", _Frozen)
    return frozen


def _restore_entry_at(when):
    """Seed one entry executed at naive-IST `when`, run the restore, return the restored position."""
    result = TradeResult(order_id="E1", status=OrderStatus.SUCCESS, message="success", timestamp=when)
    db.save(make_signal(strategy="BREAKOUT", symbol="SBIN"), _order(Action.BUY), result)
    tracker = PositionTracker(_make_engine())
    cfg = SimpleNamespace(product="MIS", exchange="NSE", strategy_profiles={"BREAKOUT": {}})
    broker = [{"symbol": "SBIN", "exchange": "NSE", "quantity": 10, "average_price": 2500.0}]
    with patch.object(startup, "settings", cfg):
        startup._restore_tracker_positions(tracker, broker, "MIS")
    return tracker.find_position("SBIN", "BREAKOUT")


def _at(frozen, hh, mm, ss=0, days_ago=0):
    from datetime import timedelta

    day = (frozen - timedelta(days=days_ago)).replace(tzinfo=None)
    return day.replace(hour=hh, minute=mm, second=ss, microsecond=0)


def test_restore_keeps_the_real_entry_time(monkeypatch):
    from signal_engine.timeutils import IST

    frozen = _freeze_db_clock(monkeypatch, 14, 0)
    when = _at(frozen, 10, 15)
    pos = _restore_entry_at(when)
    assert pos.entry_time == when.replace(tzinfo=IST)


def test_restore_entry_time_is_independent_of_the_wall_clock(monkeypatch):
    # the original test seeded now-3h and failed between 00:00 and 03:00 IST
    from signal_engine.timeutils import IST

    frozen = _freeze_db_clock(monkeypatch, 0, 30)
    when = _at(frozen, 0, 5)
    pos = _restore_entry_at(when)
    assert pos.entry_time == when.replace(tzinfo=IST)


def test_entry_at_23_59_59_yesterday_is_not_restored(monkeypatch):
    frozen = _freeze_db_clock(monkeypatch, 0, 30)
    assert _restore_entry_at(_at(frozen, 23, 59, 59, days_ago=1)) is None


def test_entry_at_00_00_01_today_is_restored_with_its_real_time(monkeypatch):
    from signal_engine.timeutils import IST

    frozen = _freeze_db_clock(monkeypatch, 0, 30)
    when = _at(frozen, 0, 0, 1)
    pos = _restore_entry_at(when)
    assert pos.entry_time == when.replace(tzinfo=IST)


def test_entry_at_exactly_midnight_today_is_restored(monkeypatch):
    # boundary: the filter is a calendar-date match (date(executed_at) = today IST), so 00:00:00 is today
    from signal_engine.timeutils import IST

    frozen = _freeze_db_clock(monkeypatch, 0, 30)
    when = _at(frozen, 0, 0, 0)
    pos = _restore_entry_at(when)
    assert pos.entry_time == when.replace(tzinfo=IST)


@pytest.mark.parametrize("now_hms", [(3, 0, 0), (23, 59, 59)])
def test_a_10_15_entry_restores_the_same_at_any_frozen_now(monkeypatch, now_hms):
    from signal_engine.timeutils import IST

    frozen = _freeze_db_clock(monkeypatch, *now_hms)
    when = _at(frozen, 10, 15)
    pos = _restore_entry_at(when)
    assert pos is not None and pos.entry_time == when.replace(tzinfo=IST)


def test_restore_puts_back_the_runner_stop_the_exit_labels_and_the_entry_context():
    sig = make_signal(strategy="BREAKOUT", symbol="SBIN")
    sig.context = {"orh": 2510.0}
    db.save(sig, _order(Action.BUY), _ok("E1"))
    leg = make_signal(strategy="BREAKOUT", symbol="SBIN", direction=Direction.EXIT)
    leg.context = {"pnl": 500.0, "exit_types": ["TP1"], "partial": True, "sl_after": 2503.0}
    db.save(leg, _order(Action.SELL), _ok("X1"))
    tracker = PositionTracker(_make_engine())
    cfg = SimpleNamespace(product="MIS", exchange="NSE", strategy_profiles={"BREAKOUT": {}})
    broker = [{"symbol": "SBIN", "exchange": "NSE", "quantity": 5, "average_price": 2500.0}]
    with patch.object(startup, "settings", cfg):
        startup._restore_tracker_positions(tracker, broker, "MIS")
    pos = tracker.find_position("SBIN", "BREAKOUT")
    assert pos.sl == 2503.0
    assert pos.exit_types == ["TP1"]
    assert pos.context == {"orh": 2510.0}
    assert pos.realized_pnl == 500.0
