"""Wording clarity fixes from the 2026-09-20 pre-live review, and the be_stop_applied tier
change - each pinned so a future edit cannot silently regress the trader-facing reason it was
made. See notifier.py's inline comments at each call site for the individual rationale.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import notifier


@pytest.mark.asyncio
async def test_no_progress_exit_is_labelled_stalled_not_time_based():
    """NO-PROGRESS EXIT and TIME EXIT are both forced closes with no SL/TP hit - without a
    qualifier a trader cannot tell "price never moved" apart from "the clock ran out" at a
    glance."""
    with patch("signal_engine.notifier.notify_event", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_no_progress_exit(
            "RELIANCE", ltp=2450.0, entry=2460.0, progress=0.05, strategy="ORB",
            direction="LONG", age_minutes=40,
        )
    msg = mock_notify.call_args.args[1]
    assert "NO-PROGRESS EXIT (stalled)" in msg


@pytest.mark.asyncio
async def test_time_exit_is_labelled_session_cutoff():
    with patch("signal_engine.notifier.notify_event", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_time_exit(
            "RELIANCE", strategy="ORB", direction="LONG", pnl=100.0, r_multiple=0.2,
            entry_price=2460.0, hold_minutes=180,
        )
    msg = mock_notify.call_args.args[1]
    assert "TIME EXIT (session cutoff)" in msg


class TestOrphanedPositionWording:
    """The old "ORDER NOT FILLED" wording read as still pending, as if a fill might still
    arrive - it will not, this is terminal. A trader on the first live day must not sit
    waiting for a fill that is never coming."""

    @pytest.mark.asyncio
    async def test_states_the_outcome_is_final(self):
        with patch("signal_engine.notifier.notify_event", new_callable=AsyncMock) as mock_notify:
            await notifier.notify_orphaned_position(
                "RELIANCE", strategy="ORB", direction="LONG", order_id="EX-1",
                reason="Entry order status could not be confirmed",
            )
        msg = mock_notify.call_args.args[1]
        assert "NO POSITION TAKEN" in msg
        assert "Final - order did not fill." in msg

    @pytest.mark.asyncio
    async def test_states_no_further_engine_action_is_coming(self):
        with patch("signal_engine.notifier.notify_event", new_callable=AsyncMock) as mock_notify:
            await notifier.notify_orphaned_position(
                "RELIANCE", strategy="ORB", direction="LONG", order_id="EX-1",
                reason="rejected",
            )
        msg = mock_notify.call_args.args[1]
        assert "No further action from the engine" in msg

    @pytest.mark.asyncio
    async def test_no_longer_says_order_not_filled(self):
        """Old wording, kept as an explicit negative assertion so a revert is caught even if
        someone only skims the positive assertions above."""
        with patch("signal_engine.notifier.notify_event", new_callable=AsyncMock) as mock_notify:
            await notifier.notify_orphaned_position(
                "RELIANCE", strategy="ORB", direction="LONG", order_id="EX-1", reason="x",
            )
        msg = mock_notify.call_args.args[1]
        assert "ORDER NOT FILLED" not in msg


class TestBeStopAppliedTier:
    """Moving the stop to break-even changes the trader's actual risk exposure - unlike the
    purely mechanical order_placed/sl_placed steps it used to share a tier with, a quiet
    channel should not hide it."""

    def test_be_stop_applied_is_delivered_at_the_quiet_level(self):
        assert notifier.should_notify("be_stop_applied", "quiet") is True

    def test_order_placed_and_sl_placed_remain_suppressed_at_quiet(self):
        """Confirms the tier change was scoped to be_stop_applied only - these two are still
        genuinely just mechanical steps."""
        assert notifier.should_notify("order_placed", "quiet") is False
        assert notifier.should_notify("sl_placed", "quiet") is False
