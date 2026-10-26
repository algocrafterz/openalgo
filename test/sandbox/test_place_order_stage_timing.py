"""Stage timing on sandbox place_order (diagnostic for the 2026-10 slow-order review).

The wrapper must log the stages of a slow call, stay silent on a fast one, and never change
what place_order returns or raises.
"""

import logging

import pytest

from sandbox import order_manager as om_module
from sandbox.order_manager import OrderManager

RESULT = (True, {"status": "success", "orderid": "X1", "mode": "analyze"}, 200)


def _manager(monkeypatch, impl):
    om = OrderManager.__new__(OrderManager)  # no FundManager / DB needed for the wrapper
    monkeypatch.setattr(om, "_place_order_impl", impl)
    return om


def test_slow_call_logs_each_stage_and_quote_call(monkeypatch, caplog):
    def impl(order_data, prefetched_quote, timer):
        timer.mark("validate")
        timer.quote(timer.start)
        timer.mark("price")
        return RESULT

    monkeypatch.setattr(om_module, "_STAGE_LOG_THRESHOLD_MS", 0.0)
    om = _manager(monkeypatch, impl)
    with caplog.at_level(logging.WARNING, logger="sandbox.order_manager"):
        assert om.place_order({"symbol": "SBIN", "price_type": "MARKET"}) == RESULT
    line = next(
        r.getMessage() for r in caplog.records if "Slow sandbox place_order" in r.getMessage()
    )
    assert "SBIN MARKET" in line
    for stage in ("validate=", "price=", "rest=", "quote_calls_ms=["):
        assert stage in line


def test_fast_call_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(om_module, "_STAGE_LOG_THRESHOLD_MS", 1e9)
    om = _manager(monkeypatch, lambda order_data, prefetched_quote, timer: RESULT)
    with caplog.at_level(logging.WARNING, logger="sandbox.order_manager"):
        assert om.place_order({"symbol": "SBIN", "price_type": "MARKET"}) == RESULT
    assert not [r for r in caplog.records if "Slow sandbox place_order" in r.getMessage()]


def test_an_exception_still_propagates_and_is_timed(monkeypatch, caplog):
    def impl(order_data, prefetched_quote, timer):
        raise RuntimeError("boom")

    monkeypatch.setattr(om_module, "_STAGE_LOG_THRESHOLD_MS", 0.0)
    om = _manager(monkeypatch, impl)
    with caplog.at_level(logging.WARNING, logger="sandbox.order_manager"):
        with pytest.raises(RuntimeError, match="boom"):
            om.place_order({"symbol": "SBIN", "price_type": "MARKET"})
    assert any("Slow sandbox place_order" in r.getMessage() for r in caplog.records)
