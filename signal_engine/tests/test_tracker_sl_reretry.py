"""Poll-loop SL re-retry: recovers positions left unprotected after the initial bracket
placement (executor.place_sl_order's 5-attempt window) exhausts inside a transient
broker-side outage (e.g. a rate-limit window). See tracker.py's _retry_missing_sl.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.models import OrderStatus, TradeResult
from signal_engine.strategies import ORB
from signal_engine.tests.tracker_fixtures import _AGED, _make_engine, _make_position
from signal_engine.tracker import PositionTracker


def _book(qty: int = 50) -> list:
    return [{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": qty, "pnl": 0}]


class TestSlReretry:
    @pytest.mark.asyncio
    async def test_retries_and_succeeds(self):
        engine = _make_engine()
        engine._state(ORB).open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(sl_order_id="")  # initial bracket already exhausted
        tracker.register(pos)

        success = TradeResult(order_id="SL123", status=OrderStatus.SUCCESS)
        with (
            patch(
                "signal_engine.tracker.fetch_positionbook",
                new_callable=AsyncMock,
                return_value=_book(),
            ),
            patch(
                "signal_engine.tracker.place_sl_order", new_callable=AsyncMock, return_value=success
            ) as mock_sl,
            patch("signal_engine.tracker.notifier.notify_sl_placed", new_callable=AsyncMock),
        ):
            await tracker.check_positions()

        mock_sl.assert_awaited_once()
        assert pos.sl_order_id == "SL123"
        assert pos.sl_retry_count == 1

    @pytest.mark.asyncio
    async def test_respects_cooldown_between_attempts(self):
        engine = _make_engine()
        engine._state(ORB).open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(sl_order_id="")
        tracker.register(pos)

        failure = TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
        with (
            patch(
                "signal_engine.tracker.fetch_positionbook",
                new_callable=AsyncMock,
                return_value=_book(),
            ),
            patch(
                "signal_engine.tracker.place_sl_order", new_callable=AsyncMock, return_value=failure
            ) as mock_sl,
            patch("signal_engine.tracker.notifier.notify_sl_placed", new_callable=AsyncMock),
            patch("signal_engine.tracker.notifier.notify_sl_failed", new_callable=AsyncMock),
        ):
            await tracker.check_positions()  # attempt 1
            await tracker.check_positions()  # too soon — cooldown not elapsed

        assert mock_sl.await_count == 1
        assert pos.sl_retry_count == 1

    @pytest.mark.asyncio
    async def test_gives_up_and_alerts_after_max_reretries(self):
        from signal_engine.config import settings

        engine = _make_engine()
        engine._state(ORB).open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(
            sl_order_id="",
            sl_retry_count=settings.bracket_max_sl_reretries - 1,
            last_sl_retry_at=_AGED - timedelta(hours=1),
        )
        tracker.register(pos)

        failure = TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
        with (
            patch(
                "signal_engine.tracker.fetch_positionbook",
                new_callable=AsyncMock,
                return_value=_book(),
            ),
            patch(
                "signal_engine.tracker.place_sl_order", new_callable=AsyncMock, return_value=failure
            ),
            patch(
                "signal_engine.tracker.notifier.notify_sl_failed", new_callable=AsyncMock
            ) as mock_notify,
        ):
            await tracker.check_positions()

        assert pos.sl_retry_count == settings.bracket_max_sl_reretries
        mock_notify.assert_awaited_once()
        assert "re-retry exhausted" in mock_notify.await_args.args[1]

    @pytest.mark.asyncio
    async def test_skips_positions_that_already_have_an_sl(self):
        engine = _make_engine()
        engine._state(ORB).open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(sl_order_id="SL_ALREADY_PLACED")
        tracker.register(pos)

        with (
            patch(
                "signal_engine.tracker.fetch_positionbook",
                new_callable=AsyncMock,
                return_value=_book(),
            ),
            patch("signal_engine.tracker.place_sl_order", new_callable=AsyncMock) as mock_sl,
        ):
            await tracker.check_positions()

        mock_sl.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_positions_not_yet_confirmed_open(self):
        engine = _make_engine()
        engine._state(ORB).open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(sl_order_id="")
        tracker.register(pos)

        with (
            patch(
                "signal_engine.tracker.fetch_positionbook",
                new_callable=AsyncMock,
                return_value=_book(qty=0),
            ),
            patch(
                "signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0
            ),
            patch("signal_engine.tracker.place_sl_order", new_callable=AsyncMock) as mock_sl,
        ):
            await tracker.check_positions()

        mock_sl.assert_not_awaited()
