"""Exit pipeline: TP HIT flow, day summary, duplicate-signal guards."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from signal_engine.main import handle_message
from signal_engine.models import (
    Direction,
    OrderStatus,
    TradeResult,
    ValidationResult,
    ValidationStatus,
)
from signal_engine.tests.pipeline_fixtures import tracker_mock


class TestExitPipelineDaySummary:
    """_handle_exit must update tracker day counters with real PnL from realised PnL delta."""

    @pytest.mark.asyncio
    async def test_exit_records_pnl_in_tracker(self):
        """Successful EXIT should call tracker.record_exit(pnl_delta, new_realised_pnl)."""
        mock_signal = MagicMock()
        mock_signal.strategy = "ORB"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "RELIANCE"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=1500.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
        ):
            # Set up tracked position
            mock_pos = MagicMock()
            mock_pos.symbol = "RELIANCE"
            mock_pos.strategy = "ORB"
            mock_pos.exchange = "NSE"
            mock_pos.product = "MIS"
            mock_pos.quantity = 50
            mock_pos.sl_order_id = "SL001"
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 1000.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0")

            # A full exit books through tracker.book_close, which files the trade
            # record and advances the day counters in one place.
            mock_tracker.book_close.assert_awaited_once()
            assert mock_tracker.book_close.await_args.kwargs["pnl_delta"] == 500.0

    @pytest.mark.asyncio
    async def test_exit_passes_real_pnl_to_risk_engine(self):
        """risk_engine.record_close should receive actual PnL, not 0.0."""
        mock_signal = MagicMock()
        mock_signal.strategy = "ORB"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "RELIANCE"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=1500.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
        ):
            mock_pos = MagicMock()
            mock_pos.symbol = "RELIANCE"
            mock_pos.strategy = "ORB"
            mock_pos.exchange = "NSE"
            mock_pos.product = "MIS"
            mock_pos.quantity = 50
            mock_pos.sl_order_id = ""
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 1000.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0")

            # record_close should receive real PnL (1500 - 1000 = 500), not hardcoded 0.0
            pnl_arg = mock_risk.record_close.call_args[1].get("pnl", mock_risk.record_close.call_args[0][0] if mock_risk.record_close.call_args[0] else None)
            assert pnl_arg != 0.0


class TestTPHitExitFlow:
    """End-to-end TP HIT signal flow: cancel SL -> MARKET SELL -> notifications."""

    @pytest.mark.asyncio
    async def test_tp_hit_cancels_sl_before_exit(self):
        """SL must be cancelled BEFORE the exit order is sent."""
        mock_signal = MagicMock()
        mock_signal.strategy = "ORB"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "RELIANCE"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP1"
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")

        call_order = []

        async def mock_cancel(order_id, strategy):
            call_order.append("cancel_sl")
            return True

        async def mock_send(order):
            call_order.append("send_exit")
            return exit_result

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", side_effect=mock_send),
            patch("signal_engine.main.cancel_order", side_effect=mock_cancel),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_pos = MagicMock()
            mock_pos.symbol = "RELIANCE"
            mock_pos.strategy = "ORB"
            mock_pos.exchange = "NSE"
            mock_pos.product = "MIS"
            mock_pos.quantity = 50
            mock_pos.sl_order_id = "SL001"
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        assert call_order == ["cancel_sl", "send_exit"]

    @pytest.mark.asyncio
    async def test_tp_hit_full_exit_fires_all_notifications(self):
        """Full TP exit should fire: exit_signal_received, exit_placed."""
        mock_signal = MagicMock()
        mock_signal.strategy = "ORB"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "RELIANCE"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP1"
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock) as mock_notifier,
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_pos = MagicMock()
            mock_pos.symbol = "RELIANCE"
            mock_pos.strategy = "ORB"
            mock_pos.exchange = "NSE"
            mock_pos.product = "MIS"
            mock_pos.quantity = 50
            mock_pos.sl_order_id = "SL001"
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_tracker._positions = {}
            mock_tracker.send_day_summary = AsyncMock()

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        mock_notifier.notify_exit_signal_received.assert_called_once_with("RELIANCE", "ORB")
        # notify_position_closed now fires inside tracker.book_close; with the tracker
        # mocked, assert the pipeline booked the close rather than the notification
        # mechanics. Real-tracker coverage lives in test_main_characterization.py.
        mock_tracker.book_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tp_hit_partial_exit_fires_partial_notification(self):
        """Partial TP exit should fire notify_partial_exit, NOT notify_exit_placed."""
        mock_signal = MagicMock()
        mock_signal.strategy = "RSI-TP-MR"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "HDFCBANK"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP1"
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock) as mock_notifier,
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_pos = MagicMock()
            mock_pos.symbol = "HDFCBANK"
            mock_pos.strategy = "RSI-TP-MR"
            mock_pos.exchange = "NSE"
            mock_pos.product = "CNC"
            mock_pos.quantity = 100
            mock_pos.entry_price = 0.0
            mock_pos.tp = 0.0  # tp=0 and entry_price=0 skip SL re-placement
            mock_pos.sl_order_id = ""
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "RSI-TP-MR": {"tp_levels": {"TP1": 0.5, "TP2": 1.0}, "product": "CNC"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("RSI-TP-MR EXIT\nSymbol: HDFCBANK\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        # Partial exit notification, not full exit
        mock_notifier.notify_partial_exit.assert_called_once()
        mock_notifier.notify_position_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_partial_exit_cancels_sl_before_exit(self):
        """Partial TP1 exit must cancel SL before placing exit order (Indian broker constraint)."""
        mock_signal = MagicMock()
        mock_signal.strategy = "RSI-TP-MR"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "HDFCBANK"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP1"
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_pos = MagicMock()
            mock_pos.symbol = "HDFCBANK"
            mock_pos.strategy = "RSI-TP-MR"
            mock_pos.exchange = "NSE"
            mock_pos.product = "CNC"
            mock_pos.quantity = 100
            mock_pos.entry_price = 0.0
            mock_pos.tp = 0.0  # tp=0 and entry_price=0 skip SL re-placement
            mock_pos.sl = 0.0
            mock_pos.sl_order_id = "SL001"
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "RSI-TP-MR": {"tp_levels": {"TP1": 0.5, "TP2": 1.0}, "product": "CNC"},
            }

            await handle_message("RSI-TP-MR EXIT\nSymbol: HDFCBANK\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        # SL MUST be cancelled before exit order — broker treats SELL while SL active as new SHORT
        mock_cancel.assert_called_once_with("SL001", "RSI-TP-MR")

    @pytest.mark.asyncio
    async def test_failed_exit_fires_failure_notification(self):
        """Failed exit order should fire notify_exit_failed."""
        mock_signal = MagicMock()
        mock_signal.strategy = "ORB"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "RELIANCE"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP1"
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        failed_result = TradeResult(order_id="", status=OrderStatus.REJECTED, message="margin error")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=failed_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_open_position", new_callable=AsyncMock, return_value=50),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock) as mock_notifier,
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_pos = MagicMock()
            mock_pos.symbol = "RELIANCE"
            mock_pos.strategy = "ORB"
            mock_pos.exchange = "NSE"
            mock_pos.product = "MIS"
            mock_pos.quantity = 50
            mock_pos.sl_order_id = "SL001"
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        mock_notifier.notify_exit_failed.assert_called_once()


class TestExitPendingGuard:
    """exit_pending flag prevents duplicate TP signals from double-processing a position."""

    @pytest.mark.asyncio
    async def test_exit_pending_blocks_duplicate_signal(self):
        """When exit_pending=True, a second EXIT signal for the same position is dropped."""
        from signal_engine.main import _handle_exit, _exit_locks
        from signal_engine.tracker import TrackedPosition

        _exit_locks.clear()

        def _make_exit_signal():
            sig = MagicMock()
            sig.strategy = "ORB"
            sig.direction = Direction.EXIT
            sig.symbol = "EXIDEIND"
            sig.entry = 0.0
            sig.sl = 0.0
            sig.tp = 0.0
            sig.tp_level = "TP1"
            sig.exit_qty_pct = 1.0
            sig.exchange = ""
            sig.product = ""
            return sig

        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")
        send_count = [0]

        with (
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result) as mock_send,
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_settings.strategy_profiles = {}
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.bracket_enabled = False

            # Create a real TrackedPosition so exit_pending works correctly
            pos = TrackedPosition(
                symbol="EXIDEIND", strategy="ORB", exchange="NSE", product="MIS",
                entry_price=320.0, quantity=58, sl=317.0, tp=323.0,
            )
            pos.exit_pending = True  # Simulate first handler already in progress

            mock_tracker.find_position.return_value = pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0

            await _handle_exit(_make_exit_signal())

        # No exit order should be placed — guard blocked it
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_exit_places_buy_order(self):
        """EXIT signal for a SHORT position must place a BUY (cover), not a SELL."""
        from signal_engine.main import _handle_exit, _exit_locks
        from signal_engine.tracker import TrackedPosition
        from signal_engine.models import Action

        _exit_locks.clear()

        sig = MagicMock()
        sig.strategy = "ORB"
        sig.direction = Direction.EXIT
        sig.symbol = "EXIDEIND"
        sig.entry = 0.0
        sig.sl = 0.0
        sig.tp = 0.0
        sig.tp_level = "TP1"
        sig.exit_qty_pct = 1.0
        sig.exchange = "NSE"
        sig.product = "MIS"

        exit_result = TradeResult(order_id="EXIT002", status=OrderStatus.SUCCESS, message="ok")
        placed_orders = []

        async def capture_order(order):
            placed_orders.append(order)
            return exit_result

        with (
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.send_order", side_effect=capture_order),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_settings.strategy_profiles = {}
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.bracket_enabled = False

            pos = TrackedPosition(
                symbol="EXIDEIND", strategy="ORB", exchange="NSE", product="MIS",
                entry_price=309.9, quantity=58, sl=312.24, tp=307.56,
                direction=Direction.SHORT,
            )

            mock_tracker.find_position.return_value = pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_tracker._positions = {"EXIDEIND:ORB": pos}
            mock_tracker.send_day_summary = AsyncMock()

            await _handle_exit(sig)

        assert len(placed_orders) == 1, "Expected exactly one exit order"
        assert placed_orders[0].action == Action.BUY, (
            f"SHORT exit must BUY to cover, got {placed_orders[0].action}"
        )


class TestConcurrentTPSignals:
    """When multiple TP alerts fire simultaneously (same bar), only the first should execute.

    TradingView fires all TP alerts at bar close when price crosses multiple levels in one bar.
    Telethon dispatches each as a separate asyncio task, so without locking both handlers
    find the same open position and place duplicate orders (phantom SHORTs on the broker).
    """

    @pytest.mark.asyncio
    async def test_concurrent_tp_signals_only_first_exits(self):
        """When TP1.5 (full exit) and TP1 (partial) arrive simultaneously, only TP1.5 executes.

        After TP1.5 fully exits and unregisters the position, the TP1 handler must find
        no position and skip — it must NOT place a second exit order or a phantom SL.
        """
        import asyncio
        from signal_engine.main import _handle_exit, _exit_locks

        # Clear any stale locks from other tests
        _exit_locks.clear()

        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")
        call_log = []

        def _make_tp_signal(tp_level: str, exit_qty_pct: float):
            sig = MagicMock()
            sig.strategy = "ORB"
            sig.direction = Direction.EXIT
            sig.symbol = "EXIDEIND"
            sig.entry = 0.0
            sig.sl = 0.0
            sig.tp = 0.0
            sig.tp_level = tp_level
            sig.exit_qty_pct = exit_qty_pct
            sig.exchange = ""
            sig.product = ""
            return sig

        tp1_5_signal = _make_tp_signal("TP1.5", 1.0)  # full exit
        tp1_signal = _make_tp_signal("TP1", 0.5)       # partial exit

        with (
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_settings.strategy_profiles = {}
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.bracket_enabled = False

            # Simulate: position exists for first call, then unregistered (returns None for second)
            mock_pos = MagicMock()
            mock_pos.symbol = "EXIDEIND"
            mock_pos.strategy = "ORB"
            mock_pos.exchange = "NSE"
            mock_pos.product = "MIS"
            mock_pos.quantity = 58
            mock_pos.sl_order_id = "SL001"
            mock_tracker._positions = {}
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0

            # First call finds position; after unregister, second call finds nothing
            find_calls = [0]
            def find_position_side_effect(symbol, strategy):
                find_calls[0] += 1
                call_log.append(f"find_position call {find_calls[0]}")
                if find_calls[0] == 1:
                    return mock_pos  # first caller gets position
                return None  # second caller — already closed

            mock_tracker.find_position.side_effect = find_position_side_effect
            mock_tracker.send_day_summary = AsyncMock()

            # Run both handlers concurrently (simulates Telethon dispatching both at once)
            await asyncio.gather(
                _handle_exit(tp1_5_signal),
                _handle_exit(tp1_signal),
            )

        # Both handlers attempted position lookup but only first found it
        assert mock_tracker.find_position.call_count == 2
        # find_position returned None for the second handler — it skipped without placing orders

    @pytest.mark.asyncio
    async def test_concurrent_tp_same_symbol_serialized(self):
        """Two simultaneous EXIT signals for same symbol must not place duplicate SL orders."""
        import asyncio
        from signal_engine.main import _handle_exit, _exit_locks

        _exit_locks.clear()

        exit_result = TradeResult(order_id="EXIT002", status=OrderStatus.SUCCESS, message="ok")
        send_order_call_count = [0]

        async def counting_send_order(order):
            send_order_call_count[0] += 1
            return exit_result

        def _make_exit_signal(tp_level: str, exit_qty_pct: float):
            sig = MagicMock()
            sig.strategy = "ORB"
            sig.direction = Direction.EXIT
            sig.symbol = "JSWENERGY"
            sig.entry = 0.0
            sig.sl = 0.0
            sig.tp = 0.0
            sig.tp_level = tp_level
            sig.exit_qty_pct = exit_qty_pct
            sig.exchange = ""
            sig.product = ""
            return sig

        sig_tp1_5 = _make_exit_signal("TP1.5", 1.0)
        sig_tp1 = _make_exit_signal("TP1", 0.5)

        with (
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", side_effect=counting_send_order),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.main.place_sl_order", new_callable=AsyncMock) as mock_place_sl,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_settings.strategy_profiles = {}
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.bracket_enabled = True

            mock_pos = MagicMock()
            mock_pos.symbol = "JSWENERGY"
            mock_pos.strategy = "ORB"
            mock_pos.exchange = "NSE"
            mock_pos.product = "MIS"
            mock_pos.quantity = 46
            mock_pos.sl_order_id = "SL002"
            mock_tracker._positions = {}
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_tracker.send_day_summary = AsyncMock()

            call_n = [0]
            def find_pos(symbol, strategy):
                call_n[0] += 1
                if call_n[0] == 1:
                    return mock_pos
                return None  # second caller sees closed position

            mock_tracker.find_position.side_effect = find_pos

            await asyncio.gather(
                _handle_exit(sig_tp1_5),
                _handle_exit(sig_tp1),
            )

        # Only 1 exit order should have been placed, not 2
        assert send_order_call_count[0] == 1, (
            f"Expected 1 exit order (duplicate TP suppressed), got {send_order_call_count[0]}"
        )
        # No phantom SL placed for a position that was already closed
        mock_place_sl.assert_not_called()
