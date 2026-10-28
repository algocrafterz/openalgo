"""Sandbox order IDs are unique by construction, not by chance.

The ID was ``YYMMDD`` + microsecond-of-second + ``random(0..99)``. That carries no time of
day, so every order placed on one day shared 10**8 possible IDs, and two orders in the same
microsecond collided with probability 1 in 100. The sequence is now milliseconds since IST
midnight, pushed forward when two orders ask in the same millisecond, so no two orders in one
process can receive the same ID.
"""

from __future__ import annotations

import threading
from datetime import datetime

import pytest
import pytz

from sandbox import order_manager
from sandbox.order_manager import OrderManager

IST = pytz.timezone("Asia/Kolkata")


class FrozenClock:
    """Stands in for the module's ``datetime``: every call sees the same instant."""

    def __init__(self, when):
        self.when = when

    def now(self, tz=None):
        return self.when


def _at(*parts):
    return FrozenClock(IST.localize(datetime(*parts)))


@pytest.fixture
def manager():
    return OrderManager.__new__(OrderManager)  # the generator needs no account or database


def test_a_frozen_clock_still_gives_every_order_its_own_id(manager, monkeypatch):
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 28, 10, 15, 3, 123456))
    monkeypatch.setattr("random.randint", lambda a, b: 7)
    ids = [manager._generate_order_id() for _ in range(2000)]
    assert len(set(ids)) == 2000


def test_the_id_keeps_its_shape_and_is_ordered_in_time(manager, monkeypatch):
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 28, 10, 15, 3, 123456))
    a = manager._generate_order_id()
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 28, 10, 15, 4, 0))
    b = manager._generate_order_id()
    assert len(a) == len(b) == 14 and a.isdigit() and b.isdigit()
    assert a.startswith("261028") and b.startswith("261028")
    assert b > a


def test_the_same_time_of_day_on_two_days_gives_different_ids(manager, monkeypatch):
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 28, 9, 30, 0, 500000))
    one = manager._generate_order_id()
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 29, 9, 30, 0, 500000))
    two = manager._generate_order_id()
    assert one != two


def test_the_clock_stepping_back_never_repeats_an_id(manager, monkeypatch):
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 28, 10, 0, 5, 0))
    a = manager._generate_order_id()
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 28, 10, 0, 4, 0))
    b = manager._generate_order_id()
    assert b != a and b > a


def test_the_last_millisecond_of_the_day_fits_the_eight_digit_sequence(manager, monkeypatch):
    monkeypatch.setattr(order_manager, "datetime", _at(2026, 10, 28, 23, 59, 59, 999000))
    ids = [manager._generate_order_id() for _ in range(50)]
    assert all(len(i) == 14 for i in ids) and len(set(ids)) == 50


def test_simultaneous_threads_never_share_an_id(manager):
    barrier = threading.Barrier(64)
    out, lock = [], threading.Lock()

    def work():
        barrier.wait()
        got = [manager._generate_order_id() for _ in range(300)]
        with lock:
            out.extend(got)

    threads = [threading.Thread(target=work) for _ in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 64 * 300 and len(set(out)) == len(out)
