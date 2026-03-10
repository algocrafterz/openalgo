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
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=mock_trade_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(sl_r, tp_r)),
            patch("signal_engine.main.save") as mock_save,
            patch("signal_engine.main.tracker") as mock_tracker,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.calculate_quantity.return_value = 50
            await handle_message(_valid_message())

            # Verify capital passed to calculate_quantity
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
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=failed_result),
            patch("signal_engine.main.save") as mock_save,
            patch("signal_engine.main.tracker") as mock_tracker,
        ):
            mock_risk.check_exposure.return_value = True
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
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(sl_result, tp_result)) as mock_bracket,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker"),
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"

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
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = False
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"

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
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"

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
            patch("signal_engine.main.build_order", return_value=mock_order),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock, return_value=(sl_result, tp_result)),
            patch("signal_engine.main.save"),
            patch("signal_engine.main.tracker") as mock_tracker,
            patch("signal_engine.main.settings") as mock_settings,
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.calculate_quantity.return_value = 50
            mock_settings.bracket_enabled = True
            mock_settings.risk_per_trade = 0.01
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"

            await handle_message(_valid_message())

        call_args = mock_tracker.register.call_args[0][0]
        assert call_args.entry_order_id == "E001"
        assert call_args.sl_order_id == "SL001"
        assert call_args.tp_order_id == "TP001"
