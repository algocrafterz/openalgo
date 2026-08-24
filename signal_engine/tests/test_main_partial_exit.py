"""Partial exits: quantity resolution and runner SL re-placement."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from signal_engine.main import handle_message, structural_runner_sl
from signal_engine.models import (
    Direction,
    OrderStatus,
    TradeResult,
    ValidationResult,
    ValidationStatus,
)
from signal_engine.tests.pipeline_fixtures import tracker_mock


class TestPartialExitFlow:
    """Multi-TP partial exit: TP1 exits a fraction of qty, keeps position registered."""

    @pytest.mark.asyncio
    async def test_tp1_exits_partial_qty(self):
        """TP1 with exit_pct=0.5 should exit 50% of position and keep it tracked."""
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
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()) as mock_build_exit,
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=1500.0),
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
            mock_pos.sl_order_id = ""
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 1000.0

            # Configure strategy profile: TP1 = 50% exit
            mock_settings.strategy_profiles = {
                "RSI-TP-MR": {"tp_levels": {"TP1": 0.5, "TP2": 1.0}, "product": "CNC"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("RSI-TP-MR EXIT\nSymbol: HDFCBANK\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

            # Should exit 50 qty (50% of 100), not full 100
            exit_qty = mock_build_exit.call_args.kwargs.get("quantity", mock_build_exit.call_args.args[2] if len(mock_build_exit.call_args.args) > 2 else None)
            assert exit_qty == 50

            # Position should NOT be fully unregistered (partial exit)
            mock_tracker.unregister.assert_not_called()

    @pytest.mark.asyncio
    async def test_tp2_exits_remaining_full_qty(self):
        """TP2 with exit_pct=1.0 should exit remaining qty and unregister position."""
        mock_signal = MagicMock()
        mock_signal.strategy = "RSI-TP-MR"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "HDFCBANK"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP2"
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT002", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()) as mock_build_exit,
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=2000.0),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_pos = MagicMock()
            mock_pos.symbol = "HDFCBANK"
            mock_pos.strategy = "RSI-TP-MR"
            mock_pos.exchange = "NSE"
            mock_pos.product = "CNC"
            mock_pos.quantity = 50  # remaining after TP1 took 50
            mock_pos.sl_order_id = ""
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 1500.0

            mock_settings.strategy_profiles = {
                "RSI-TP-MR": {"tp_levels": {"TP1": 0.5, "TP2": 1.0}, "product": "CNC"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("RSI-TP-MR EXIT\nSymbol: HDFCBANK\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP2")

            # Full remaining qty exit
            exit_qty = mock_build_exit.call_args.kwargs.get("quantity", mock_build_exit.call_args.args[2] if len(mock_build_exit.call_args.args) > 2 else None)
            assert exit_qty == 50

            # Should be fully unregistered
            mock_tracker.unregister.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_tp_level_exits_full_position(self):
        """EXIT without tp_level (safety EXIT, not TP HIT) should exit full qty."""
        mock_signal = MagicMock()
        mock_signal.strategy = "RSI-TP-MR"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "HDFCBANK"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = None
        mock_signal.exchange = ""
        mock_signal.product = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT003", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()) as mock_build_exit,
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=2000.0),
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
            mock_pos.sl_order_id = ""
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 1500.0

            mock_settings.strategy_profiles = {
                "RSI-TP-MR": {"tp_levels": {"TP1": 0.5, "TP2": 1.0}, "product": "CNC"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("RSI-TP-MR EXIT\nSymbol: HDFCBANK\nEntry: 0.0\nSL: 0.0\nTP: 0.0")

            # Full qty exit — no tp_level means close everything
            exit_qty = mock_build_exit.call_args.kwargs.get("quantity", mock_build_exit.call_args.args[2] if len(mock_build_exit.call_args.args) > 2 else None)
            assert exit_qty == 100
            mock_tracker.unregister.assert_called_once()


class TestPartialExitSlReplacement:
    """After partial exit, a new SL-M must be placed for the remaining position qty."""

    def _make_partial_exit_context(self, sl_order_id="SL001", sl_price=2485.0, bracket_enabled=True):
        """Helper: set up mocks for an ORB partial exit scenario."""
        from signal_engine.models import Direction as _Direction

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

        mock_pos = MagicMock()
        mock_pos.symbol = "RELIANCE"
        mock_pos.strategy = "ORB"
        mock_pos.exchange = "NSE"
        mock_pos.product = "MIS"
        mock_pos.quantity = 100
        mock_pos.entry_price = 2500.0
        mock_pos.tp = 2525.0  # TP1 price — new SL after partial exit (locks TP1 profit on remaining qty)
        mock_pos.sl = sl_price
        mock_pos.direction = _Direction.LONG
        mock_pos.sl_order_id = sl_order_id

        return mock_signal, mock_pos

    @pytest.mark.asyncio
    async def test_sl_replayed_for_remaining_qty_after_partial_exit(self):
        """After TP1 partial exit (50%), a new SL-M must be placed for remaining 50 shares."""
        from signal_engine.models import Direction as _Direction

        mock_signal, mock_pos = self._make_partial_exit_context(sl_order_id="SL001", sl_price=2485.0)
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")
        new_sl_result = TradeResult(order_id="SL002", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
            patch("signal_engine.main.place_sl_order", new_callable=AsyncMock, return_value=new_sl_result) as mock_place_sl,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = True
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.tp1_runner_sl_buffer = 0.1  # 10% of R below TP1

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        # SL = TP1 - 0.1R = 2525 - 0.1*(2525-2500) = 2525 - 2.5 = 2522.5
        mock_place_sl.assert_called_once()
        call_kwargs = mock_place_sl.call_args.kwargs
        assert call_kwargs["symbol"] == "RELIANCE"
        assert call_kwargs["quantity"] == 50  # remaining after 50% partial exit
        assert call_kwargs["sl_price"] == pytest.approx(2522.5)  # TP1 - 0.1R buffer
        assert call_kwargs["direction"] == _Direction.LONG

        # sl_order_id updated to new SL order
        assert mock_pos.sl_order_id == "SL002"

    @pytest.mark.asyncio
    async def test_sl_falls_back_to_entry_when_tp_missing(self):
        """Defensive: if pos.tp is 0/missing, fall back to entry_price (old breakeven behaviour)."""
        mock_signal, mock_pos = self._make_partial_exit_context(sl_order_id="SL001", sl_price=2485.0)
        mock_pos.tp = 0.0  # simulate missing TP on position (shouldn't happen in practice)
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")
        new_sl_result = TradeResult(order_id="SL002", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
            patch("signal_engine.main.place_sl_order", new_callable=AsyncMock, return_value=new_sl_result) as mock_place_sl,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = True
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.tp1_runner_sl_buffer = 0.1

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        mock_place_sl.assert_called_once()
        assert mock_place_sl.call_args.kwargs["sl_price"] == 2500.0  # fallback: no buffer when tp=0

    @pytest.mark.asyncio
    async def test_sl_replacement_failure_logs_error_and_notifies(self):
        """If SL re-placement fails after partial exit, error is logged and Telegram notified."""
        mock_signal, mock_pos = self._make_partial_exit_context(sl_order_id="SL001")
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")
        failed_sl_result = TradeResult(order_id="", status=OrderStatus.REJECTED, message="margin error")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
            patch("signal_engine.main.place_sl_order", new_callable=AsyncMock, return_value=failed_sl_result),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock) as mock_notifier,
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = True
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.tp1_runner_sl_buffer = 0.1

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        # sl_order_id remains empty — no protection, but position is still tracked
        assert mock_pos.sl_order_id == ""
        # Telegram notification fired to alert operator
        mock_notifier.notify_sl_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_sl_not_replayed_when_bracket_disabled(self):
        """When bracket_enabled=False, no SL re-placement attempt after partial exit."""
        mock_signal, mock_pos = self._make_partial_exit_context(sl_order_id="")
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
            patch("signal_engine.main.place_sl_order", new_callable=AsyncMock) as mock_place_sl,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = False  # brackets disabled

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        mock_place_sl.assert_not_called()

    @pytest.mark.asyncio
    async def test_runner_sl_buffer_applied_for_short_position(self):
        """SHORT position: runner SL must be TP1 + 0.1R (above TP1, not below)."""
        from signal_engine.models import Direction as _Direction

        mock_signal = MagicMock()
        mock_signal.strategy = "ORB"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "AXISBANK"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP1"
        mock_signal.exchange = ""
        mock_signal.product = ""

        mock_pos = MagicMock()
        mock_pos.symbol = "AXISBANK"
        mock_pos.strategy = "ORB"
        mock_pos.exchange = "NSE"
        mock_pos.product = "MIS"
        mock_pos.quantity = 100
        mock_pos.entry_price = 1100.0   # SHORT: entry above TP
        mock_pos.tp = 1075.0            # TP1 below entry for short (R = 25)
        mock_pos.sl = 1115.0
        mock_pos.direction = _Direction.SHORT
        mock_pos.sl_order_id = "SL001"

        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT001", status=OrderStatus.SUCCESS, message="ok")
        new_sl_result = TradeResult(order_id="SL002", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine"),
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
            patch("signal_engine.main.place_sl_order", new_callable=AsyncMock, return_value=new_sl_result) as mock_place_sl,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = True
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_settings.tp1_runner_sl_buffer = 0.1

            await handle_message("ORB EXIT\nSymbol: AXISBANK\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        # SHORT: TP1 = 1075, entry = 1100, R = 25, buffer = 0.1 * 25 = 2.5
        # new SL = TP1 + buffer = 1075 + 2.5 = 1077.5 (above TP for short = closer to entry)
        mock_place_sl.assert_called_once()
        assert mock_place_sl.call_args.kwargs["sl_price"] == pytest.approx(1077.5)

    @pytest.mark.asyncio
    async def test_tp15_exits_remaining_and_no_sl_replayed(self):
        """TP1.5 is a full exit of remaining position — no SL re-placement needed."""
        from signal_engine.models import Direction as _Direction

        mock_signal = MagicMock()
        mock_signal.strategy = "ORB"
        mock_signal.direction = Direction.EXIT
        mock_signal.symbol = "RELIANCE"
        mock_signal.entry = 0.0
        mock_signal.sl = 0.0
        mock_signal.tp = 0.0
        mock_signal.tp_level = "TP1.5"
        mock_signal.exchange = ""
        mock_signal.product = ""

        mock_pos = MagicMock()
        mock_pos.symbol = "RELIANCE"
        mock_pos.strategy = "ORB"
        mock_pos.exchange = "NSE"
        mock_pos.product = "MIS"
        mock_pos.quantity = 50  # remaining after TP1 partial exit
        mock_pos.sl = 2485.0
        mock_pos.direction = _Direction.LONG
        mock_pos.sl_order_id = "SL002"

        valid_result = ValidationResult(status=ValidationStatus.VALID)
        exit_result = TradeResult(order_id="EXIT002", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.tracker", new_callable=tracker_mock) as mock_tracker,
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.build_exit_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=exit_result),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.main.fetch_realised_pnl", new_callable=AsyncMock, return_value=1000.0),
            patch("signal_engine.main.place_sl_order", new_callable=AsyncMock) as mock_place_sl,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_tracker.find_position.return_value = mock_pos
            mock_tracker._time_exit_active = False
            mock_tracker._last_realised_pnl = 500.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = True
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0
            mock_tracker._positions = {}
            mock_tracker.send_day_summary = AsyncMock()

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1.5")

        # Full exit at TP1.5 — position unregistered, no SL re-placement
        mock_tracker.unregister.assert_called_once_with("RELIANCE", "ORB")
        mock_place_sl.assert_not_called()


class TestStructuralRunnerStop:
    """After TP1 the runner's stop belongs at the level that justified the trade, not at a
    fixed fraction of R. The thesis is "price accepted beyond VAH"; if price falls back
    through VAH the thesis is dead. Floored at entry so a booked winner can never turn into
    a loser."""

    def test_long_uses_trigger_level_when_above_entry(self):
        # VAH 183.83 sits BELOW entry 184.04, so break-even wins.
        assert structural_runner_sl(
            direction=Direction.LONG, entry_price=184.04, tp=184.88,
            context={"trigger": "VAH-RT", "vah": "183.83"},
        ) == 184.04

    def test_long_uses_level_when_it_sits_above_entry(self):
        assert structural_runner_sl(
            direction=Direction.LONG, entry_price=184.04, tp=184.88,
            context={"trigger": "VAH-RT", "vah": "184.50"},
        ) == 184.50

    def test_short_floors_at_entry(self):
        assert structural_runner_sl(
            direction=Direction.SHORT, entry_price=275.85, tp=274.30,
            context={"trigger": "IBL-RT", "ibl": "276.10"},
        ) == 275.85

    def test_short_uses_level_when_it_sits_below_entry(self):
        assert structural_runner_sl(
            direction=Direction.SHORT, entry_price=275.85, tp=274.30,
            context={"trigger": "IBL-RT", "ibl": "275.00"},
        ) == 275.00

    def test_falls_back_to_none_without_a_trigger(self):
        """A plain ORB breakout carries no key level — caller keeps the TP1-buffer rule."""
        assert structural_runner_sl(
            direction=Direction.LONG, entry_price=184.04, tp=184.88, context={},
        ) is None

    def test_falls_back_when_level_price_absent(self):
        assert structural_runner_sl(
            direction=Direction.LONG, entry_price=184.04, tp=184.88,
            context={"trigger": "VAH-RT"},
        ) is None

    def test_falls_back_on_unparseable_level_price(self):
        assert structural_runner_sl(
            direction=Direction.LONG, entry_price=184.04, tp=184.88,
            context={"trigger": "VAH-RT", "vah": "-"},
        ) is None

    def test_trigger_family_is_read_before_the_suffix(self):
        """Trigger is FAMILY-VARIANT, e.g. PDH-BRK; only the family names a level."""
        assert structural_runner_sl(
            direction=Direction.LONG, entry_price=100.0, tp=102.0,
            context={"trigger": "PDH-BRK", "pdh": "101.0"},
        ) == 101.0
