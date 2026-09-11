"""C2 enforcement in the poll loop, and M1: the unrealised-drawdown gate is finally wired.

C2 — the poll loop is the only thing that runs continuously all session, so it is where a
mid-session OpenAlgo mode flip gets noticed.

M1 — RiskEngine.update_unrealised() had no production caller at all. `unrealised_loss` was
permanently 0.0, so check_exposure()'s `daily_realised_loss + unrealised_loss` was just the
realised half, while PRD.md and the project notes both described the combined gate as live.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import mode_guard
from signal_engine.models import Direction
from signal_engine.tracker import BookEntry, PositionTracker, TrackedPosition


@pytest.fixture(autouse=True)
def _clean_guard():
    mode_guard.reset()
    yield
    mode_guard.reset()


def _pos(symbol="SBIN", strategy="ORB", qty=100, fill=800.0, direction=Direction.LONG):
    return TrackedPosition(
        symbol=symbol, strategy=strategy, exchange="NSE", product="MIS",
        entry_price=fill, quantity=qty, sl=796.0, tp=810.0, direction=direction,
        entry_order_id="1", fill_price=fill, ever_seen_nonzero_qty=True,
    )


class TestUnrealisedIsPushedToTheRiskEngine:
    def test_long_position_underwater_reports_a_loss(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos(qty=100, fill=800.0))
        # LTP 790 on 100 shares long = -1,000 unrealised
        tracker._push_unrealised({"SBIN": BookEntry(100, 790.0)})
        risk.update_unrealised.assert_called_once_with(1000.0, "ORB")

    def test_short_position_underwater_reports_a_loss(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos(qty=100, fill=800.0, direction=Direction.SHORT))
        # LTP 810 on 100 shares short = -1,000 unrealised
        tracker._push_unrealised({"SBIN": BookEntry(-100, 810.0)})
        risk.update_unrealised.assert_called_once_with(1000.0, "ORB")

    def test_a_position_in_profit_reports_zero_not_a_negative_loss(self):
        """The limit adds this to realised loss, so a winner must not credit against it."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos(qty=100, fill=800.0))
        tracker._push_unrealised({"SBIN": BookEntry(100, 815.0)})
        risk.update_unrealised.assert_called_once_with(0.0, "ORB")

    def test_losses_are_summed_per_strategy(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos(symbol="SBIN", strategy="ORB", qty=100, fill=800.0))
        tracker.register(_pos(symbol="TCS", strategy="ORB", qty=10, fill=4000.0))
        tracker.register(_pos(symbol="INFY", strategy="BREAKOUT", qty=50, fill=1500.0))
        tracker._push_unrealised({
            "SBIN": BookEntry(100, 790.0),    # -1,000
            "TCS": BookEntry(10, 3950.0),     # -500
            "INFY": BookEntry(50, 1480.0),    # -1,000
        })
        by_strategy = {c.args[1]: c.args[0] for c in risk.update_unrealised.call_args_list}
        assert by_strategy == {"ORB": 1500.0, "BREAKOUT": 1000.0}

    def test_a_strategy_with_nothing_open_is_reset_to_zero(self):
        """A stale unrealised figure would keep throttling a strategy whose position closed."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos(qty=100, fill=800.0))
        tracker._push_unrealised({"SBIN": BookEntry(100, 790.0)})
        risk.update_unrealised.reset_mock()
        tracker.unregister("SBIN", "ORB")
        tracker._push_unrealised({})
        risk.update_unrealised.assert_called_once_with(0.0, "ORB")

    def test_a_missing_or_zero_ltp_is_skipped_not_treated_as_a_total_loss(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos(qty=100, fill=800.0))
        tracker._push_unrealised({"SBIN": BookEntry(100, 0.0)})
        risk.update_unrealised.assert_called_once_with(0.0, "ORB")


class TestModeFlipIsNoticedByThePollLoop:
    @pytest.mark.asyncio
    async def test_poll_checks_for_a_flip(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        with patch("signal_engine.mode_guard.check_for_flip", AsyncMock(return_value=None)) as chk:
            await tracker._check_mode_flip()
        chk.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_flip_raises_a_critical_alert(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        mode_guard.halt("mode changed from ANALYZE to LIVE")
        with patch("signal_engine.mode_guard.check_for_flip", AsyncMock(return_value="live")), \
             patch("signal_engine.notifier.notify_event", AsyncMock()) as notify:
            await tracker._check_mode_flip()
        notify.assert_awaited_once()
        assert notify.await_args.args[0] == "mode_flip_halt"
