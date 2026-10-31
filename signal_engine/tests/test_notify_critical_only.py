"""notify_level `alerts` (2026-10-31): only failures and unscheduled stops or starts reach Telegram."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import notifier
from signal_engine.timeutils import IST

FRI = datetime(2026, 10, 30, tzinfo=IST)


def _at(hour, minute, day=FRI):
    return day.replace(hour=hour, minute=minute)


@pytest.fixture
def alerts_level(monkeypatch):
    monkeypatch.setattr(notifier, "settings", SimpleNamespace(notify_level="alerts"))


def test_routine_declines_and_banners_are_classified_quiet():
    assert notifier.EVENT_LEVELS["entry_declined"] == "quiet"
    assert notifier.EVENT_LEVELS["routine_status"] == "quiet"
    assert not notifier.should_notify("entry_declined", "alerts")
    assert notifier.should_notify("order_rejected", "alerts")  # a real rejection is never filtered


@pytest.mark.parametrize(
    ("when", "sent"),
    [
        (_at(9, 10), False),  # scheduled engine start
        (_at(15, 35), False),  # scheduled engine stop
        (_at(9, 15), True),  # a start at the open is a restart
        (_at(12, 40), True),  # mid-session stop or restart
        (_at(15, 29), True),
        (_at(12, 40, datetime(2026, 10, 31, tzinfo=IST)), False),  # Saturday
    ],
)
def test_a_banner_goes_out_only_inside_market_hours(alerts_level, when, sent):
    assert notifier.routine_status_enabled(when) is sent


def test_banners_follow_the_level_outside_market_hours(monkeypatch):
    monkeypatch.setattr(notifier, "settings", SimpleNamespace(notify_level="quiet"))
    assert notifier.routine_status_enabled(_at(16, 0)) is True


@pytest.mark.asyncio
async def test_a_clean_scheduled_start_is_not_sent_but_a_failed_one_is(alerts_level):
    send = AsyncMock()
    with (
        patch.object(notifier, "_send_oneshot", send),
        patch.object(notifier, "routine_status_enabled", return_value=False),
        patch.object(notifier, "build_startup_summary_message", return_value="summary"),
    ):
        await notifier.notify_startup_summary(SimpleNamespace(all_passed=True), "analyze", 1.0, "b")
        send.assert_not_awaited()
        await notifier.notify_startup_summary(
            SimpleNamespace(all_passed=False), "analyze", 1.0, "b"
        )
        send.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_entry_decline_is_dropped_at_alerts(monkeypatch):
    monkeypatch.setattr(
        notifier,
        "settings",
        SimpleNamespace(notify_level="alerts", notify_channel={"analyze": {"id": 1}}),
    )
    with patch.object(notifier, "notify_event", wraps=notifier.notify_event) as event:
        await notifier.notify_entry_declined("TATASTEEL", "below the minimum price", strategy="ORB")
    assert event.await_args.args[0] == "entry_declined"
    assert await notifier.notify("x", event="entry_declined") is False
