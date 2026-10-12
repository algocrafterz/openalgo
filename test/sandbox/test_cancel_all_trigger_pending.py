"""cancelallorder must cancel SL/SL-M orders and must not report success while orders remain working.

2026-10-12: sandbox_cancel_all_orders selected orders with status "trigger_pending" (underscore) while
order_manager stores SL/SL-M orders as "trigger pending" (space, upstream 3e7132a52, 2026-07-24). Every
working stop was skipped and the call answered success ("No open orders to cancel"). After the 14:45
signal_engine time exit a PAYTM SL-M stayed working on a flat position and opened an unintended long at
15:12:51 (signal_engine/docs/INCIDENT-2026-10-12-TIME-EXIT-STOP-NOT-CANCELLED.md).
"""

import uuid
from datetime import datetime

import pytest
import pytz

from database.sandbox_db import SandboxOrders, db_session
from sandbox.order_manager import OrderManager
from services import sandbox_service

IST = pytz.timezone("Asia/Kolkata")
USER_ID = "cancel-all-trigger-pending-user"


@pytest.fixture(autouse=True)
def _user(monkeypatch):
    monkeypatch.setattr(sandbox_service, "get_user_id_from_apikey", lambda key: USER_ID)
    SandboxOrders.query.filter_by(user_id=USER_ID).delete()
    db_session.commit()
    yield
    SandboxOrders.query.filter_by(user_id=USER_ID).delete()
    db_session.commit()


def _seed(status: str, price_type: str = "SL-M", action: str = "BUY") -> str:
    order_id = uuid.uuid4().hex[:14]
    now = datetime.now(IST)
    db_session.add(
        SandboxOrders(
            orderid=order_id, user_id=USER_ID, symbol="RELIANCE", exchange="NSE", action=action,
            quantity=1, price=0 if price_type != "LIMIT" else 1, trigger_price=1 if "SL" in price_type else 0,
            price_type=price_type, product="MIS", order_status=status, average_price=None,
            filled_quantity=0, pending_quantity=1, margin_blocked=0, strategy="EVWAP-EARLY-PY",
            order_timestamp=now, update_timestamp=now,
        )
    )
    db_session.commit()
    return order_id


def _status(order_id: str) -> str:
    db_session.expire_all()
    return SandboxOrders.query.filter_by(orderid=order_id).first().order_status


def _cancel_all():
    return sandbox_service.sandbox_cancel_all_orders({}, "test-key", {})


def test_trigger_pending_stop_and_open_limit_are_cancelled():
    stop = _seed("trigger pending")
    limit = _seed("open", price_type="LIMIT")
    done = _seed("complete", price_type="MARKET")
    ok, resp, code = _cancel_all()
    assert ok is True and code == 200 and resp["status"] == "success"
    assert set(resp["canceled_orders"]) == {stop, limit}
    assert _status(stop) == "cancelled" and _status(limit) == "cancelled"
    assert _status(done) == "complete"  # finished orders are left alone


def test_no_working_orders_is_still_success():
    _seed("complete", price_type="MARKET")
    ok, resp, code = _cancel_all()
    assert ok is True and resp["status"] == "success" and resp["canceled_orders"] == []


def test_partial_failure_follows_the_documented_contract(monkeypatch):
    # docs/api/order-management/cancelallorder.md "Partial Success": status success means the
    # request was processed; failed_cancellations names the orders still working (same as live).
    stop = _seed("trigger pending")
    limit = _seed("open", price_type="LIMIT")
    real_cancel = OrderManager.cancel_order

    def cancel(self, oid):
        if oid == stop:
            return False, {"message": "boom"}, 500
        return real_cancel(self, oid)

    monkeypatch.setattr(OrderManager, "cancel_order", cancel)
    ok, resp, code = _cancel_all()
    assert ok is True and code == 200 and resp["status"] == "success"
    assert resp["canceled_orders"] == [limit]
    assert [f["orderid"] for f in resp["failed_cancellations"]] == [stop]
    assert "Failed to cancel 1 orders" in resp["message"]
    assert _status(stop) == "trigger pending"  # still working, and the caller is told so
