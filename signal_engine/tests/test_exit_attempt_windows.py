"""BUG-019 replay: 2026-11-04 AXISBANK SELL 154 sent three times by EVWAP-EARLY-PY.

Every send timed out client-side while the core was still working on it (a 168 s quote lookup).
The late-fill window was started once, at the first send, so attempt 2 had no window left and
attempt 3 went out ~0.5 s later while attempt 2 was still queued. Each attempt now has its own
window, an in-flight rejection from the core is reconciled instead of resent, and a window that
ends without a decision is logged.

A virtual clock drives the whole replay (sends take 5 s, reads advance time by their delay), so a
120 s window is exercised for real instead of being cut short by stubbed sleeps.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from signal_engine import order_reconcile as oc
from signal_engine.main import _send_exit_with_retries
from signal_engine.models import Action, Direction, Order, OrderStatus, TradeResult
from signal_engine.timeutils import IST

SETTINGS = SimpleNamespace(
    bracket_tp_exit_retries=3, bracket_retry_delay=0.0,
    entry_timeout_recovery_attempts=3, entry_timeout_recovery_delay_seconds=0.0,
)
SEND_SECONDS = 5
INFLIGHT = "Duplicate order in flight: an identical SELL 154 AXISBANK order is still being processed"


class World:
    """The core and the broker book, on a virtual clock."""

    def __init__(self, copy_delay, core_guard):
        self.now = datetime(2026, 11, 4, 13, 50, 0, tzinfo=IST)
        self.t0 = self.now
        self.copy_delay = copy_delay  # a copy appears on the book this long after its send
        self.core_guard = core_guard  # the core refuses a copy while an earlier one is in flight
        self.copies = []  # (order id, placed_at)
        self.send_times = []

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def elapsed(self, when):
        return (when - self.t0).total_seconds()

    async def send(self, order):
        self.send_times.append(self.now)
        started = self.now
        self.advance(SEND_SECONDS)
        if self.core_guard and any(placed > self.now for _, placed in self.copies):
            return TradeResult(status=OrderStatus.REJECTED, message=INFLIGHT)
        self.copies.append((f"C{len(self.copies) + 1}", started + timedelta(seconds=self.copy_delay)))
        return TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")

    async def book(self):
        return [
            {"orderid": oid, "symbol": "AXISBANK", "action": "SELL", "quantity": 154,
             "order_status": "complete", "timestamp": placed.strftime("%Y-%m-%d %H:%M:%S"),
             "strategy": "EVWAP-EARLY-PY"}
            for oid, placed in self.copies if placed <= self.now
        ]

    async def sleep(self, seconds):
        self.advance(seconds)


def _clock(world):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return world.now

    return FakeDT


async def _replay(world, own_qty=0):
    order = Order(symbol="AXISBANK", exchange="NSE", action=Action.SELL, quantity=154, price=0.0,
                  order_type="MARKET", product="MIS", strategy_tag="EVWAP-EARLY-PY")
    pos = SimpleNamespace(symbol="AXISBANK", strategy="EVWAP-EARLY-PY", quantity=154,
                          exchange="NSE", product="MIS", direction=Direction.LONG)
    notify = AsyncMock()
    with (
        patch("signal_engine.main.settings", SETTINGS),
        patch("signal_engine.main.datetime", _clock(world)),
        patch("signal_engine.main.send_order", AsyncMock(side_effect=world.send)),
        patch("signal_engine.main.fetch_orderbook", AsyncMock(side_effect=world.book)),
        patch("signal_engine.main.fetch_order_ids_today", MagicMock(return_value=set())),
        patch("signal_engine.main.notifier", MagicMock(notify=notify)),
        patch("signal_engine.main.tracker",
              MagicMock(fetch_own_position=AsyncMock(return_value=own_qty))),
        patch("signal_engine.main._watch_for_late_duplicate", MagicMock()),
        patch("signal_engine.order_reconcile.asyncio", SimpleNamespace(sleep=world.sleep)),
    ):
        result = await _send_exit_with_retries(order, pos)
    return result, notify


@pytest.fixture(autouse=True)
def _clean_registry():
    oc._uncertain.clear()
    yield
    oc._uncertain.clear()


@pytest.mark.asyncio
class TestReplay20261104:
    async def test_attempt_two_gets_its_own_window_and_adopts_the_first_copy(self):
        """Old core (no guard). Copy 1 lands 150 s after send 1, inside attempt 2's own window.
        Before the fix attempt 2 had no window and attempt 3 went out about 5 s after it."""
        world = World(copy_delay=150, core_guard=False)
        result, _ = await _replay(world)
        assert len(world.send_times) == 2, [world.elapsed(t) for t in world.send_times]
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "C1")

    async def test_every_attempt_waits_its_own_window_before_the_next_send(self):
        world = World(copy_delay=10**6, core_guard=False)  # nothing ever lands
        await _replay(world)
        gaps = [world.elapsed(b) - world.elapsed(a)
                for a, b in zip(world.send_times, world.send_times[1:])]
        assert len(world.send_times) == 3
        assert all(gap >= 120 for gap in gaps), gaps  # send + a full 120 s window each

    async def test_core_in_flight_rejection_is_reconciled_not_resent(self):
        """New core: copy 1 is still being placed when attempt 2 arrives and is refused. The engine
        must wait for that copy and adopt it: one copy ever created, one fill, two sends."""
        world = World(copy_delay=150, core_guard=True)
        result, _ = await _replay(world)
        assert len(world.copies) == 1
        assert len(world.send_times) == 2
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "C1")

    async def test_in_flight_rejection_with_no_copy_ever_visible_holds_and_alerts(self):
        """The core keeps reporting a copy in flight that never reaches the book: after the last
        attempt's window the exit is held and the UNPROTECTED alert goes out; nothing is resent."""
        world = World(copy_delay=10**6, core_guard=True)
        result, notify = await _replay(world)
        assert len(world.send_times) == 3 and len(world.copies) == 1
        assert result.status == OrderStatus.TIMEOUT and "UNCERTAIN" in result.message
        assert "UNPROTECTED" in str(notify.await_args_list)
        assert oc.get_uncertain("EVWAP-EARLY-PY", "AXISBANK") is not None

    async def test_a_window_that_ends_without_a_decision_is_logged(self):
        world = World(copy_delay=10**6, core_guard=False)
        lines = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
        try:
            await _replay(world)
        finally:
            logger.remove(sink)
        ended = [m for m in lines if "window ended" in m]
        assert len(ended) == 3, lines
