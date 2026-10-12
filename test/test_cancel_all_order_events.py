"""The "All Orders Cancelled" event is published only when every working order was cancelled.

2026-10-12: sandbox cancel-all left every SL-M working while reporting success, and the service
published AllOrdersCancelledEvent regardless of the result, with counts read from keys the response
never carries (always 0). A failure must reach subscribers as a failure (OrderFailedEvent), and the
counts must come from the canceled / failed lists.
"""

from types import SimpleNamespace
from unittest.mock import patch

from events import AllOrdersCancelledEvent, OrderFailedEvent
from services import cancel_all_order_service as svc


def _run_sandbox(result):
    published = []
    with (
        patch.object(svc, "get_analyze_mode", return_value=True),
        patch("services.sandbox_service.sandbox_cancel_all_orders", return_value=result),
        patch.object(svc.bus, "publish", side_effect=published.append),
    ):
        out = svc.cancel_all_orders_with_auth({}, "token", "flattrade", {"apikey": "k"})
    return out, published


def _run_live(canceled, failed):
    published = []
    broker = SimpleNamespace(cancel_all_orders_api=lambda data, token: (canceled, failed))
    with (
        patch.object(svc, "get_analyze_mode", return_value=False),
        patch.object(svc, "import_broker_module", return_value=broker),
        patch.object(svc.bus, "publish", side_effect=published.append),
    ):
        out = svc.cancel_all_orders_with_auth({}, "token", "flattrade", {"apikey": "k"})
    return out, published


def test_sandbox_confirmed_cancel_publishes_all_cancelled_with_real_counts():
    resp = {"status": "success", "canceled_orders": ["A", "B"], "failed_cancellations": []}
    (ok, _, code), events = _run_sandbox((True, resp, 200))
    assert ok and code == 200
    [event] = events
    assert isinstance(event, AllOrdersCancelledEvent)
    assert (event.canceled_count, event.failed_count) == (2, 0)


def test_sandbox_partial_failure_publishes_a_failure_not_all_cancelled():
    resp = {
        "status": "error",
        "message": "Canceled 1 orders. Failed to cancel 1 orders.",
        "canceled_orders": ["A"],
        "failed_cancellations": [{"orderid": "B", "message": "boom"}],
    }
    (ok, _, code), events = _run_sandbox((False, resp, 500))
    assert not ok and code == 500
    assert not any(isinstance(e, AllOrdersCancelledEvent) for e in events)
    [event] = events
    assert isinstance(event, OrderFailedEvent) and "B" in event.error_message


def test_live_partial_failure_publishes_a_failure_but_keeps_the_response():
    (ok, resp, code), events = _run_live(["A"], [{"orderid": "B", "message": "rejected"}])
    assert ok and code == 200 and resp["failed_cancellations"]  # response contract unchanged
    assert not any(isinstance(e, AllOrdersCancelledEvent) for e in events)
    [event] = events
    assert isinstance(event, OrderFailedEvent) and "1 of 2" in event.error_message


def test_live_confirmed_cancel_is_unchanged():
    (ok, _, code), events = _run_live(["A", "B"], [])
    assert ok and code == 200
    [event] = events
    assert isinstance(event, AllOrdersCancelledEvent) and event.canceled_count == 2
