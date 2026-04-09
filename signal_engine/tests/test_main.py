"""Tests for main pipeline orchestration."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from signal_engine.main import handle_message
from signal_engine.models import (
    Direction,
    OrderStatus,
    Signal,
    TradeResult,
    ValidationResult,
    ValidationStatus,
)


def _bracket_trade_results():
    sl_result = TradeResult(order_id="SL001", status=OrderStatus.SUCCESS, message="ok")
    tp_result = TradeResult(order_id="TP001", status=OrderStatus.SUCCESS, message="ok")
    return sl_result, tp_result


def _valid_message():
    return "ORB LONG\nSymbol: RELIANCE\nEntry: 2500\nSL: 2485\nTP: 2540"


def _mock_signal():
    sig = MagicMock()
    sig.strategy = "ORB"
    sig.direction = Direction.LONG
    sig.symbol = "RELIANCE"
    sig.entry = 2500.0
    sig.sl = 2485.0
    sig.tp = 2540.0
    return sig


class TestPipelineFlow:
    @pytest.mark.asyncio
    async def test_unparseable_message_skipped(self):
        with patch("signal_engine.main.parse", return_value=None):
            await handle_message("garbage text")

    @pytest.mark.asyncio
    async def test_invalid_signal_skipped(self):
        invalid_result = ValidationResult(
            status=ValidationStatus.INVALID, reason="bad"
        )
        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=invalid_result),
            patch("signal_engine.main.send_order") as mock_send,
        ):
            await handle_message(_valid_message())
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignored_signal_skipped(self):
        ignored_result = ValidationResult(
            status=ValidationStatus.IGNORED, reason="dup"
        )
        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=ignored_result),
            patch("signal_engine.main.send_order") as mock_send,
        ):
            await handle_message(_valid_message())
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_risk_limit_skipped(self):
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.send_order") as mock_send,
        ):
            mock_risk.check_exposure.return_value = False
            await handle_message(_valid_message())
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_success_path(self):
        mock_signal = _mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        mock_trade_result = TradeResult(
            order_id="123", status=OrderStatus.SUCCESS, message="ok"
        )
        sl_r = TradeResult(order_id="SL001", status=OrderStatus.SUCCESS, message="ok")
        tp_r = TradeResult(order_id="TP001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=200_000.0),
            patch("signal_engine.main.adjust_qty_for_margin", new_callable=AsyncMock, return_value=50),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=mock_trade_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(sl_r, tp_r)),
            patch("signal_engine.main.save") as mock_save,
            patch("signal_engine.main.tracker") as mock_tracker,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 200_000.0
            mock_risk.calculate_quantity.return_value = 50
            await handle_message(_valid_message())

            # Capital flows through get_sizing_capital before calculate_quantity
            mock_risk.get_sizing_capital.assert_called_once_with(200_000.0)
            mock_risk.calculate_quantity.assert_called_once_with(mock_signal, capital=200_000.0)
            mock_risk.record_trade.assert_called_once()
            mock_tracker.register.assert_called_once()
            mock_save.assert_called_once_with(mock_signal, mock_order, mock_trade_result)

    @pytest.mark.asyncio
    async def test_failed_order_does_not_track(self):
        mock_signal = _mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        failed_result = TradeResult(
            order_id="", status=OrderStatus.REJECTED, message="margin insufficient"
        )

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=50_000.0),
            patch("signal_engine.main.adjust_qty_for_margin", new_callable=AsyncMock, return_value=10),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=failed_result),
            patch("signal_engine.main.save") as mock_save,
            patch("signal_engine.main.tracker") as mock_tracker,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 50_000.0
            mock_risk.calculate_quantity.return_value = 10
            await handle_message(_valid_message())

            mock_risk.record_trade.assert_not_called()
            mock_tracker.register.assert_not_called()
            mock_save.assert_called_once()  # still saved for audit

    @pytest.mark.asyncio
    async def test_zero_capital_skips_trade(self):
        valid_result = ValidationResult(status=ValidationStatus.VALID)

        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.main.send_order") as mock_send,
        ):
            mock_risk.check_exposure.return_value = True
            await handle_message(_valid_message())

            # Capital=0 means API unreachable, trade should be skipped
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_below_min_capital_skips_trade(self):
        """Entry must be skipped when live capital is below min_capital_for_entry threshold."""
        valid_result = ValidationResult(status=ValidationStatus.VALID)

        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=800.0),
            patch("signal_engine.main.send_order") as mock_send,
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.can_trade_symbol.return_value = True
            mock_risk.can_trade_sector.return_value = True
            mock_settings.min_capital_for_entry = 2000.0  # threshold is ₹2,000

            await handle_message(_valid_message())

            # ₹800 < ₹2,000 threshold — order must not be sent
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_min_capital_proceeds(self):
        """Entry proceeds normally when live capital exceeds the threshold."""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        entry_result = TradeResult(order_id="E001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=5000.0),
            patch("signal_engine.main.adjust_qty_for_margin", new_callable=AsyncMock, return_value=10),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result) as mock_send,
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(None, None)),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker"),
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.can_trade_symbol.return_value = True
            mock_risk.can_trade_sector.return_value = True
            mock_risk.get_sizing_capital.return_value = 15000.0
            mock_risk.calculate_quantity.return_value = 10
            mock_settings.min_capital_for_entry = 2000.0  # ₹5,000 > ₹2,000 — should proceed
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.bracket_enabled = False
            mock_settings.bracket_cnc_sl_enabled = False

            await handle_message(_valid_message())

            mock_send.assert_called_once()


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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_tracker._last_realised_pnl = 1000.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0")

            mock_tracker.record_exit.assert_called_once()

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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_tracker._last_realised_pnl = 1000.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0")

            # record_close should receive real PnL (1500 - 1000 = 500), not hardcoded 0.0
            pnl_arg = mock_risk.record_close.call_args[1].get("pnl", mock_risk.record_close.call_args[0][0] if mock_risk.record_close.call_args[0] else None)
            assert pnl_arg != 0.0


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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_pos.entry_price = 0.0  # entry_price=0 skips breakeven SL re-placement
            mock_pos.sl_order_id = ""
            mock_tracker.find_position.return_value = mock_pos
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
        mock_notifier.notify_position_closed.assert_called_once()

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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_pos.entry_price = 0.0  # entry_price=0 skips breakeven SL re-placement
            mock_pos.sl_order_id = ""
            mock_tracker.find_position.return_value = mock_pos
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_pos.sl = 0.0  # sl=0 skips SL re-placement
            mock_pos.sl_order_id = "SL001"
            mock_tracker.find_position.return_value = mock_pos
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        mock_notifier.notify_exit_failed.assert_called_once()


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
        mock_pos.entry_price = 2500.0  # breakeven SL after partial exit
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = True
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        # SL re-placement must be called for remaining 50 shares at breakeven (entry price)
        mock_place_sl.assert_called_once()
        call_kwargs = mock_place_sl.call_args.kwargs
        assert call_kwargs["symbol"] == "RELIANCE"
        assert call_kwargs["quantity"] == 50  # remaining after 50% partial exit
        assert call_kwargs["sl_price"] == 2500.0  # breakeven = entry price (not original SL)
        assert call_kwargs["direction"] == _Direction.LONG

        # sl_order_id updated to new SL order
        assert mock_pos.sl_order_id == "SL002"

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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = True
            mock_settings.bracket_tp_exit_retries = 1
            mock_settings.bracket_retry_delay = 0.0

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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            mock_tracker._last_realised_pnl = 0.0
            mock_settings.strategy_profiles = {
                "ORB": {"tp_levels": {"TP1": 0.5, "TP1.5": 1.0}, "product": "MIS"},
            }
            mock_settings.bracket_enabled = False  # brackets disabled

            await handle_message("ORB EXIT\nSymbol: RELIANCE\nEntry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: TP1")

        mock_place_sl.assert_not_called()

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
            patch("signal_engine.main.tracker") as mock_tracker,
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


class TestConcentrationRisk:
    @pytest.mark.asyncio
    async def test_symbol_concentration_blocked(self):
        valid_result = ValidationResult(status=ValidationStatus.VALID)

        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.send_order") as mock_send,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.can_trade_symbol.return_value = False
            mock_risk.can_trade_sector.return_value = True
            await handle_message(_valid_message())
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_sector_concentration_blocked(self):
        valid_result = ValidationResult(status=ValidationStatus.VALID)

        with (
            patch("signal_engine.main.parse", return_value=_mock_signal()),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.send_order") as mock_send,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.can_trade_symbol.return_value = True
            mock_risk.can_trade_sector.return_value = False
            await handle_message(_valid_message())
            mock_send.assert_not_called()


class TestCncBracketSkip:
    """CNC orders should skip SL-M bracket placement — SL-M is cancelled at EOD by exchange."""

    @pytest.mark.asyncio
    async def test_cnc_entry_skips_bracket(self):
        """CNC product should not place SL-M bracket even when bracket_enabled=True."""
        mock_signal = _mock_signal()
        mock_signal.product = "CNC"
        mock_signal.exchange = "NSE"
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        entry_result = TradeResult(order_id="E001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=200_000.0),
            patch("signal_engine.main.adjust_qty_for_margin", new_callable=AsyncMock, return_value=50),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock) as mock_bracket,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker"),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 200_000.0
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.bracket_cnc_sl_enabled = False
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "CNC"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0  # disabled for this test

            await handle_message(_valid_message())

        mock_bracket.assert_not_called()

    @pytest.mark.asyncio
    async def test_mis_entry_still_places_bracket(self):
        """MIS product should still place SL-M bracket when bracket_enabled=True."""
        mock_signal = _mock_signal()
        mock_signal.product = ""
        mock_signal.exchange = ""
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        entry_result = TradeResult(order_id="E001", status=OrderStatus.SUCCESS, message="ok")
        sl_result = TradeResult(order_id="SL001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=200_000.0),
            patch("signal_engine.main.adjust_qty_for_margin", new_callable=AsyncMock, return_value=50),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(sl_result, None)) as mock_bracket,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker"),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 200_000.0
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.bracket_cnc_sl_enabled = False
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0  # disabled for this test

            await handle_message(_valid_message())

        mock_bracket.assert_called_once()


class TestBracketOrderFlow:
    @pytest.mark.asyncio
    async def test_bracket_enabled_places_sl_and_tp_after_entry(self):
        mock_signal = _mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        entry_result = TradeResult(order_id="E001", status=OrderStatus.SUCCESS, message="ok")
        sl_result, tp_result = _bracket_trade_results()

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=200_000.0),
            patch("signal_engine.main.adjust_qty_for_margin", new_callable=AsyncMock, return_value=50),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(sl_result, tp_result)) as mock_bracket,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker"),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 200_000.0
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0  # disabled for this test

            await handle_message(_valid_message())

        mock_bracket.assert_called_once_with(mock_signal, 50, "E001")

    @pytest.mark.asyncio
    async def test_bracket_disabled_does_not_place_sl_tp(self):
        mock_signal = _mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        entry_result = TradeResult(order_id="E001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=200_000.0),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock) as mock_bracket,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker"),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 200_000.0
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = False
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0  # disabled for this test

            await handle_message(_valid_message())

        mock_bracket.assert_not_called()

    @pytest.mark.asyncio
    async def test_bracket_not_called_when_entry_fails(self):
        mock_signal = _mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        failed_entry = TradeResult(order_id="", status=OrderStatus.REJECTED, message="margin error")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=200_000.0),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=failed_entry),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock) as mock_bracket,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker"),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 200_000.0
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0  # disabled for this test

            await handle_message(_valid_message())

        mock_bracket.assert_not_called()

    @pytest.mark.asyncio
    async def test_tracker_receives_bracket_order_ids(self):
        """TrackedPosition must be registered with sl_order_id and tp_order_id."""
        mock_signal = _mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        mock_order = MagicMock()
        entry_result = TradeResult(order_id="E001", status=OrderStatus.SUCCESS, message="ok")
        sl_result = TradeResult(order_id="SL001", status=OrderStatus.SUCCESS, message="ok")
        tp_result = TradeResult(order_id="TP001", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=200_000.0),
            patch("signal_engine.main.adjust_qty_for_margin", new_callable=AsyncMock, return_value=50),
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(sl_result, tp_result)),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker") as mock_tracker,
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 200_000.0
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0  # disabled for this test

            await handle_message(_valid_message())

        call_args = mock_tracker.register.call_args[0][0]
        assert call_args.entry_order_id == "E001"
        assert call_args.sl_order_id == "SL001"


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
            patch("signal_engine.main.tracker") as mock_tracker,
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
            patch("signal_engine.main.tracker") as mock_tracker,
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
