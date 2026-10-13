"""2026-10-13 HINDALCO: broker recovery must not derive a position while another strategy is mid-exit.

Timeline replayed here. BREAKOUT-PY held 320 HINDALCO and began its TP1 exit. At 10:00:28 its SELL 320
filled, so the broker net was 0, but the tracker still held +320 for it until 10:00:30. At 10:00:26-29 a
Pine BREAKOUT exit reached recovery: net 0 minus other-strategy +320 = -320, read as a short, and a BUY
320 went out. It opened an unmanaged long that only the 14:45 flatten closed, for -752.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine.main import _recover_position_from_broker
from signal_engine.models import Direction, Signal
from signal_engine.risk import RiskEngine
from signal_engine.tracker import PositionTracker, TrackedPosition


def _tracker_with_py_position(exit_pending: bool) -> PositionTracker:
    tracker = PositionTracker(MagicMock(spec=RiskEngine))
    tracker.register(
        TrackedPosition(
            symbol="HINDALCO", strategy="BREAKOUT-PY", exchange="NSE", product="MIS",
            entry_price=890.0, quantity=320, sl=880.0, tp=0.0, direction=Direction.LONG,
            exit_pending=exit_pending,
        )
    )
    return tracker


class TestExitInFlight:
    def test_true_while_another_strategy_is_mid_exit_on_the_same_position(self):
        t = _tracker_with_py_position(exit_pending=True)
        assert t.other_strategy_exit_in_flight("HINDALCO", "NSE", "MIS", "BREAKOUT") is True

    def test_false_when_the_other_strategy_is_not_exiting(self):
        t = _tracker_with_py_position(exit_pending=False)
        assert t.other_strategy_exit_in_flight("HINDALCO", "NSE", "MIS", "BREAKOUT") is False

    def test_a_strategys_own_exit_does_not_block_itself(self):
        t = _tracker_with_py_position(exit_pending=True)
        assert t.other_strategy_exit_in_flight("HINDALCO", "NSE", "MIS", "BREAKOUT-PY") is False

    def test_another_symbol_is_unaffected(self):
        t = _tracker_with_py_position(exit_pending=True)
        assert t.other_strategy_exit_in_flight("TCS", "NSE", "MIS", "BREAKOUT") is False


def _exit_signal():
    return Signal(strategy="BREAKOUT", direction=Direction.EXIT, symbol="HINDALCO", entry=0.0, sl=0.0,
                  tp=0.0, raw_message="BREAKOUT TP3 HIT", sig_id="HINDALCO-20261013-1000")


@pytest.mark.asyncio
class TestRecoveryReplay:
    async def test_mid_exit_window_derives_nothing_so_no_buy_320_is_sent(self):
        # Net already 0 (the SELL 320 filled); tracker still holds +320 and is mid-exit.
        t = _tracker_with_py_position(exit_pending=True)
        with (
            patch("signal_engine.main.tracker", t),
            patch("signal_engine.main.settings", SimpleNamespace(exchange="NSE", product="MIS")),
            patch("signal_engine.main.notifier", MagicMock(notify_exit_no_position=AsyncMock())),
            patch("signal_engine.tracker.fetch_net_position", AsyncMock(return_value=0)),
        ):
            assert await _recover_position_from_broker(_exit_signal()) is None

    async def test_after_the_other_strategy_unregisters_recovery_sees_a_flat_book(self):
        t = PositionTracker(MagicMock(spec=RiskEngine))
        notifier = MagicMock(notify_exit_no_position=AsyncMock())
        with (
            patch("signal_engine.main.tracker", t),
            patch("signal_engine.main.settings", SimpleNamespace(exchange="NSE", product="MIS")),
            patch("signal_engine.main.notifier", notifier),
            patch("signal_engine.tracker.fetch_net_position", AsyncMock(return_value=0)),
        ):
            assert await _recover_position_from_broker(_exit_signal()) is None
        notifier.notify_exit_no_position.assert_awaited_once()

    async def test_a_genuine_restart_recovery_with_no_one_exiting_still_works(self):
        t = PositionTracker(MagicMock(spec=RiskEngine))
        with (
            patch("signal_engine.main.tracker", t),
            patch("signal_engine.main.settings", SimpleNamespace(exchange="NSE", product="MIS")),
            patch("signal_engine.main.notifier", MagicMock(notify_exit_no_position=AsyncMock())),
            patch("signal_engine.tracker.fetch_net_position", AsyncMock(return_value=320)),
            patch("signal_engine.main._recover_entry_context", return_value=(890.0, 880.0, 0.0, "E1")),
        ):
            pos = await _recover_position_from_broker(_exit_signal())
        assert pos is not None and pos.quantity == 320 and pos.direction == Direction.LONG
