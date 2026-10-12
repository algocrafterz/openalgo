"""Order Book "Cancel all" answer: a failed cancellation is never shown as "No open orders".

2026-10-12: with every cancellation failing, the UI endpoint answered {"status": "info",
"message": "No open orders to cancel"} and dropped the failed list, so stops left working looked
like an empty book.
"""

from blueprints.orders import _cancel_all_ui_payload


def test_nothing_to_cancel_is_info():
    assert _cancel_all_ui_payload({"canceled_orders": [], "failed_cancellations": []}) == {
        "status": "info",
        "message": "No open orders to cancel",
    }


def test_every_cancel_failed_keeps_the_failed_list():
    failed = [{"orderid": "SL1", "message": "boom"}]
    out = _cancel_all_ui_payload({"canceled_orders": [], "failed_cancellations": failed})
    assert out["failed_cancellations"] == failed
    assert out["message"] == "Canceled 0 orders. Failed to cancel 1 orders"


def test_partial_failure_keeps_both_lists():
    out = _cancel_all_ui_payload(
        {"canceled_orders": ["A", "B"], "failed_cancellations": [{"orderid": "C"}]}
    )
    assert out["canceled_orders"] == ["A", "B"] and out["failed_cancellations"] == [{"orderid": "C"}]
    assert "Failed to cancel 1" in out["message"]


def test_full_success_is_unchanged():
    out = _cancel_all_ui_payload({"canceled_orders": ["A"], "failed_cancellations": []})
    assert out == {
        "status": "success",
        "message": "Successfully canceled 1 orders",
        "canceled_orders": ["A"],
        "failed_cancellations": [],
    }
