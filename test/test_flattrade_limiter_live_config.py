"""The Flattrade data limiter at the values the live .env sets, under a queued history burst.

test_flattrade_limiter_priority.py proves the lane mechanics at the broker's 9/110. This file
pins the configuration actually deployed (7/s, 110/min, reserve 2/10, background 1/20, 3s
background bound) so a change to those numbers cannot silently lose the guarantee: an order or a
stop-loss poll is served ahead of queued history, and the broker's own caps are never exceeded.
Time is faked, so every figure is exact.
"""

from __future__ import annotations

import pytest

from broker.flattrade.api import rate_limit
from broker.flattrade.api.rate_limit import SlidingWindowLimiter
from utils import broker_priority
from utils.broker_backpressure import BrokerBusyError

LIVE = {
    "max_per_second": 7,
    "max_per_minute": 110,
    "reserve_per_second": 2,
    "reserve_per_minute": 10,
    "background_per_second": 1,
    "background_per_minute": 20,
    "background_max_wait": 3.0,
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(rate_limit, "time", fake)
    return fake


def _book(limiter, clock, priority):
    """Absolute second the call leaves, or None when a background call is refused."""
    try:
        return clock.now + limiter.reserve(priority)
    except BrokerBusyError:
        return None


def _assert_caps(slots, per_s, per_m):
    slots = sorted(slots)
    for i, start in enumerate(slots):
        assert sum(1 for s in slots[i:] if s < start + 1.0) <= per_s
        assert sum(1 for s in slots[i:] if s < start + 60.0) <= per_m


def test_orders_are_served_ahead_of_a_queued_history_burst(clock):
    limiter = SlidingWindowLimiter("data", **LIVE)
    history = [_book(limiter, clock, broker_priority.NORMAL) for _ in range(60)]
    assert max(history) - clock.now > 5.0  # the burst really is queued, not served at once

    orders = [_book(limiter, clock, broker_priority.CRITICAL) for _ in range(4)]

    assert all(o is not None for o in orders)
    assert max(orders) - clock.now <= 1.0
    assert max(orders) < max(history)  # served ahead of the queued tail


def test_background_valuation_cannot_delay_a_stop_loss_poll(clock):
    limiter = SlidingWindowLimiter("data", **LIVE)
    for _ in range(80):
        _book(limiter, clock, broker_priority.BACKGROUND)

    polls = [_book(limiter, clock, broker_priority.CRITICAL) for _ in range(6)]

    assert max(polls) - clock.now <= 1.0


def test_background_is_refused_rather_than_queued_past_the_bound(clock):
    limiter = SlidingWindowLimiter("data", **LIVE)
    results = [_book(limiter, clock, broker_priority.BACKGROUND) for _ in range(60)]

    assert any(r is None for r in results)
    assert all(r - clock.now <= LIVE["background_max_wait"] for r in results if r is not None)


def test_mixed_load_never_exceeds_the_broker_caps(clock):
    limiter = SlidingWindowLimiter("data", **LIVE)
    slots = []
    for _ in range(40):
        clock.now += 1.0
        for priority in (
            broker_priority.NORMAL, broker_priority.NORMAL, broker_priority.NORMAL,
            broker_priority.BACKGROUND, broker_priority.BACKGROUND,
            broker_priority.CRITICAL,
        ):
            slot = _book(limiter, clock, priority)
            if slot is not None:
                slots.append(slot)

    _assert_caps(slots, 7, 110)
