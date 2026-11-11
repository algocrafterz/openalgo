"""E-11: state before notification.

_establish_position awaited notify_entry_filled BEFORE tracker.register. The notifier had no
timeout and the Telethon client sleeps inline on a flood wait (up to 120 s), so for that long
the position had no close detection and no time exit.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from signal_engine import notifier
from signal_engine.main import _establish_position
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.tests.tracker_fixtures import _make_engine
from signal_engine.tracker import PositionTracker


def _signal():
    return Signal(
        strategy="BREAKOUT-PY", direction=Direction.LONG, symbol="PFC", entry=325.6, sl=324.6,
        tp=327.0, raw_message="x", sig_id="BREAKOUT-PY-PFC-1",
    )


def _order():
    return Order(
        symbol="PFC", exchange="NSE", action=Action.BUY, quantity=930, price=0.0,
        order_type="MARKET", product="MIS", strategy_tag="BREAKOUT-PY",
    )


@pytest.mark.asyncio
async def test_position_is_registered_before_a_blocked_entry_notice_returns():
    real_tracker = PositionTracker(_make_engine())
    blocked = asyncio.Event()

    async def blocking_notice(*_a, **_k):
        blocked.set()
        await asyncio.sleep(60)  # a flood wait inside the Telegram client

    fake_notifier = MagicMock()
    fake_notifier.notify_entry_filled = blocking_notice
    settings = SimpleNamespace(
        exchange="NSE", product="MIS", max_entry_slippage_r=0.5, bracket_enabled=True
    )
    entry = TradeResult(status=OrderStatus.SUCCESS, order_id="ENTRY-1")
    with (
        patch("signal_engine.main.settings", settings),
        patch("signal_engine.main.notifier", fake_notifier),
        patch("signal_engine.main.tracker", real_tracker),
        patch("signal_engine.main.risk_engine", MagicMock()),
        patch("signal_engine.main._place_entry_bracket", AsyncMock(return_value="SL-1")),
        patch("signal_engine.main._fetch_entry_fill", AsyncMock(return_value=325.7)),
        patch("signal_engine.main.update_entry_fill", MagicMock()),
        patch("signal_engine.main._auto_close_on_bad_fill", AsyncMock(return_value=None)),
    ):
        task = asyncio.create_task(_establish_position(_signal(), _order(), 930, entry, entry_row_id=1))
        try:
            await asyncio.wait_for(blocked.wait(), 5)
            await asyncio.sleep(0.05)
            pos = real_tracker.find_position("PFC", "BREAKOUT-PY")
            assert pos is not None, "position was not tracked while the entry notice was blocked"
            assert pos.sl_order_id == "SL-1" and pos.fill_price == 325.7
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_a_send_that_exceeds_the_bound_returns_false_and_logs(monkeypatch):
    lines = []
    sink_id = logger.add(lambda m: lines.append(m.record["level"].name + " " + m.record["message"]))

    async def hang(_channel, _text):
        await asyncio.sleep(60)

    client = SimpleNamespace(send_message=hang)
    channel = SimpleNamespace(id=1, name="chan")
    monkeypatch.setattr(notifier, "_client", client)
    monkeypatch.setattr(notifier, "_SEND_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(notifier, "_current_phase", AsyncMock(return_value="analyze"))
    monkeypatch.setattr(notifier, "_channel_for_phase", lambda _p: channel)
    monkeypatch.setattr(
        notifier, "settings", SimpleNamespace(notify_channel="x", notify_level="normal")
    )
    try:
        started = asyncio.get_running_loop().time()
        delivered = await asyncio.wait_for(notifier.notify("hello", event="entry_filled"), 5)
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        logger.remove(sink_id)
    assert delivered is False
    assert elapsed < 2
    assert any(line.startswith("WARNING") and "entry_filled" in line for line in lines)
