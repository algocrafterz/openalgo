"""G6: a no-progress exit that did not happen leaves no NO-PROGRESS label and reads truthfully."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.models import OrderStatus, TradeResult
from signal_engine.tests.log_capture import capturing
from signal_engine.tests.test_exit_status_verification import _np_tracker


async def _run(send_result, status=""):
    tracker, pos = _np_tracker()
    pos.exit_types.append("TP1")  # an earlier label that must survive
    stop = TradeResult(order_id="SL_NEW", status=OrderStatus.SUCCESS, message="ok")
    with (
        patch("signal_engine.main._send_exit_with_retries", new_callable=AsyncMock, return_value=send_result),
        patch("signal_engine.tracker.fetch_order_fill_price", new_callable=AsyncMock, return_value=None),
        patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value=status),
        patch("signal_engine.tracker.place_sl_order", new_callable=AsyncMock, return_value=stop),
        capturing() as logged,
    ):
        await tracker._no_progress_market_exit(pos, timedelta(minutes=40), 2490.0, 2500.0, 0.1)
    # The former sl-failed Telegram alert is now an ERROR log line starting "SL FAILED | ".
    return pos, [m for m in logged.messages("ERROR") if m.startswith("SL FAILED | ")]


@pytest.mark.asyncio
async def test_an_exit_that_could_not_be_placed_leaves_no_label():
    failed = TradeResult(status=OrderStatus.ERROR, message="broker down")
    pos, sl_failed = await _run(failed)
    assert pos.exit_types == ["TP1"]
    assert len(sl_failed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "cancelled", "cancel"])
async def test_an_exit_rejected_after_acceptance_leaves_no_label(status):
    ok = TradeResult(order_id="EXNP", status=OrderStatus.SUCCESS, message="ok")
    pos, _ = await _run(ok, status)
    assert pos.exit_types == ["TP1"]
    assert pos.sl_order_id == "SL_NEW"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "word"), [("cancel", "cancelled"), ("cancelled", "cancelled"), ("rejected", "rejected")]
)
async def test_the_alert_names_the_status_in_plain_words(status, word):
    ok = TradeResult(order_id="EXNP", status=OrderStatus.SUCCESS, message="ok")
    _, sl_failed = await _run(ok, status)
    (text,) = sl_failed
    assert f"was {word} by the broker" in text
    assert "was cancel by" not in text


@pytest.mark.asyncio
async def test_a_working_exit_keeps_its_label():
    ok = TradeResult(order_id="EXNP", status=OrderStatus.SUCCESS, message="ok")
    pos, _ = await _run(ok, "open")
    assert "NO-PROGRESS" in pos.exit_types
