"""Slow sandbox fills report where the time went: waiting for the position lock, or working in it.

2026-10-30: bursts of simultaneous orders took 3 s then 12 s each in the fill ("execute") stage with
no broker delay. Every fill holds one process-wide lock across its commit and margin check, so the
log has to separate waiting for the lock from the work done holding it.
"""

import logging
import time

from sandbox.execution_engine import _FillTimer


def test_summary_names_every_part():
    timer = _FillTimer()
    timer.lock_acquired()
    timer.mark("update_commit")
    timer.mark("margin_check")
    timer.mark("reconcile")
    text = timer.summary()
    for part in ("lock_wait=", "held=", "update_commit=", "margin_check=", "reconcile="):
        assert part in text


def test_lock_wait_is_measured_before_the_lock_is_acquired():
    timer = _FillTimer()
    time.sleep(0.05)
    timer.lock_acquired()
    assert timer.lock_wait_ms >= 45


def test_a_fast_fill_is_not_logged(caplog):
    timer = _FillTimer()
    timer.lock_acquired()
    with caplog.at_level(logging.WARNING):
        timer.log_if_slow("SBIN", "ORD1", threshold_ms=1000)
    assert not caplog.records


def test_a_slow_fill_is_logged_with_its_symbol_and_order(caplog):
    timer = _FillTimer()
    timer.lock_acquired()
    with caplog.at_level(logging.WARNING):
        timer.log_if_slow("SBIN", "ORD1", threshold_ms=0)
    assert any("SBIN" in r.getMessage() and "ORD1" in r.getMessage() for r in caplog.records)


def test_an_unmeasured_lock_does_not_break_the_summary():
    # The fill raised before it got the lock; the timer must still report instead of failing.
    assert "lock_wait=" in _FillTimer().summary()
