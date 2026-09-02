"""Regression: six notifications converted to log-only on 2026-04-22 were restored to
Telegram on 2026-09-02 for channel-based trade analysis. Each must call notify() with a
non-empty message — pins the fix so a future edit cannot silently drop the Telegram send
back to log-only again without a test failing.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import notifier


@pytest.mark.asyncio
async def test_entry_filled_sends_telegram():
    with patch("signal_engine.notifier.notify", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_entry_filled(
            "RELIANCE", "LONG", 2501.5, 50, 2500.0,
            strategy="ORB", sl=2485.0, tp=2540.0,
        )
    mock_notify.assert_awaited_once()
    assert "RELIANCE" in mock_notify.call_args.args[0]


@pytest.mark.asyncio
async def test_sl_placed_sends_telegram():
    with patch("signal_engine.notifier.notify", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_sl_placed("RELIANCE", "SL001", strategy="ORB", sl_price=2485.0)
    mock_notify.assert_awaited_once()
    assert "SL001" in mock_notify.call_args.args[0]


@pytest.mark.asyncio
async def test_partial_exit_sends_telegram():
    with patch("signal_engine.notifier.notify", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_partial_exit(
            "RELIANCE", 50, 50, "TP1", 500.0,
            strategy="ORB", entry_price=2500.0, new_sl=2522.5,
            next_tp_label="TP1.5", next_tp_price=2537.5,
            direction="LONG", r_multiple=0.7, hold_minutes=12,
        )
    mock_notify.assert_awaited_once()
    msg = mock_notify.call_args.args[0]
    assert "TP1" in msg and "RELIANCE" in msg


@pytest.mark.asyncio
async def test_position_closed_sends_telegram():
    with patch("signal_engine.notifier.notify", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_position_closed(
            "RELIANCE", 900.0, strategy="ORB", exit_price=2540.0,
            direction="LONG", r_multiple=1.5, entry_price=2500.0,
            hold_minutes=45, exit_types=["TP2"], day_context="Day: 1/8 trades (W:1 L:0)",
        )
    mock_notify.assert_awaited_once()
    msg = mock_notify.call_args.args[0]
    assert "RELIANCE" in msg and "Day: 1/8" in msg


@pytest.mark.asyncio
async def test_be_stop_applied_sends_telegram_and_shows_original_sl():
    """Also pins the original_sl fix: the message must show the PRIOR stop, not None."""
    with patch("signal_engine.notifier.notify", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_be_stop_applied(
            "RELIANCE", 2500.0, 2505.0, 0.35,
            strategy="ORB", direction="LONG", age_minutes=60,
            entry_price=2500.0, original_sl=2485.0,
        )
    mock_notify.assert_awaited_once()
    msg = mock_notify.call_args.args[0]
    assert "2485.00" in msg and "2500.00" in msg


@pytest.mark.asyncio
async def test_no_progress_exit_sends_telegram():
    with patch("signal_engine.notifier.notify", new_callable=AsyncMock) as mock_notify:
        await notifier.notify_no_progress_exit(
            "RELIANCE", 2495.0, 2500.0, 0.20,
            strategy="ORB", direction="LONG", age_minutes=90,
        )
    mock_notify.assert_awaited_once()
    assert "RELIANCE" in mock_notify.call_args.args[0]
