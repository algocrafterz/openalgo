"""exit_owner: a position whose exits belong to the runner's Ladder is left to it."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.models import Direction
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import BookEntry, PositionTracker

_IST = timezone(timedelta(hours=5, minutes=30))


def _settings(owner: str):
    s = SimpleNamespace(
        no_progress_enabled=True,
        no_progress_check_after_minutes=90,
        no_progress_min_progress_pct=0.33,
        no_progress_profit_lock_ratio=0.40,
        no_progress_early_check_enabled=False,
        no_progress_early_check_after_minutes=45,
        no_progress_early_min_progress_pct=0.05,
        no_progress_use_fill_price=True,
        no_progress_chop_tightener_enabled=False,
        no_progress_chop_tightener_trigger_count=2,
        no_progress_chop_tightener_early_check_after_minutes=30,
        no_progress_loss_cut_enabled=False,
        time_exit_enabled=False,
        runner=SimpleNamespace(
            strategies={"ORB": SimpleNamespace(exit_owner=owner)},
        ),
    )
    return s


async def _run(owner: str) -> AsyncMock:
    tracker = PositionTracker(_make_engine())
    pos = _make_position(
        entry_price=541.80,
        fill_price=541.80,
        sl=528.01,
        tp=551.10,
        direction=Direction.LONG,
        entry_time=datetime.now(_IST) - timedelta(minutes=150),
    )
    tracker.register(pos)
    book = {"RELIANCE": BookEntry(1, 544.70)}
    with (
        patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
        patch("signal_engine.tracker.place_sl_order") as place,
        patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock),
        patch("signal_engine.config.settings", _settings(owner)),
    ):
        from signal_engine.models import OrderStatus, TradeResult

        place.return_value = TradeResult(status=OrderStatus.SUCCESS, order_id="N", message="")
        await tracker._check_no_progress(book)
    return place


class TestExitOwner:
    @pytest.mark.asyncio
    async def test_tracker_owned_position_still_gets_no_progress_action(self):
        place = await _run("tracker")
        place.assert_called_once()

    @pytest.mark.asyncio
    async def test_ladder_owned_position_is_left_alone(self):
        place = await _run("ladder")
        place.assert_not_called()
