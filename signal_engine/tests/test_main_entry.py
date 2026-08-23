"""Entry pipeline: dispatch, risk gates, bracket placement, fill handling."""

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
from signal_engine.tests.pipeline_fixtures import (
    _bracket_trade_results,
    _mock_signal,
    _valid_message,
)


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
            mock_settings.slippage_factor = 0.10
            mock_settings.max_sl_pct_for_sizing = 0.0
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.bracket_enabled = False
            mock_settings.bracket_cnc_sl_enabled = False

            await handle_message(_valid_message())

            mock_send.assert_called_once()


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
            mock_settings.slippage_factor = 0.10
            mock_settings.max_sl_pct_for_sizing = 0.0
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
            mock_settings.slippage_factor = 0.10
            mock_settings.max_sl_pct_for_sizing = 0.0
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
            mock_settings.slippage_factor = 0.10
            mock_settings.max_sl_pct_for_sizing = 0.0
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
        """TrackedPosition must be registered with sl_order_id."""
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
            mock_settings.slippage_factor = 0.10
            mock_settings.max_sl_pct_for_sizing = 0.0
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0  # disabled for this test

            await handle_message(_valid_message())

        call_args = mock_tracker.register.call_args[0][0]
        assert call_args.entry_order_id == "E001"
        assert call_args.sl_order_id == "SL001"


class TestFillOvershotTP:
    """Fill price exceeds TP at entry (high slippage) — position must auto-close immediately.

    Reproduces the VBL Apr-29 bug: MARKET order filled at 533.95 vs TP 532.75.
    System must cancel SL, send market close, and NOT register in tracker.
    """

    def _mock_signal(self, direction=Direction.LONG, entry=528.0, sl=519.34, tp=532.75):
        sig = MagicMock()
        sig.strategy = "ORB"
        sig.symbol = "VBL"
        sig.direction = direction
        sig.entry = entry
        sig.sl = sl
        sig.tp = tp
        sig.exchange = "NSE"
        sig.product = "MIS"
        return sig

    @pytest.mark.asyncio
    async def test_long_fill_above_tp_auto_closes(self):
        """LONG fill above TP → SL cancelled, market close placed, tracker not registered."""
        mock_signal = self._mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        entry_result = TradeResult(order_id="E_VBL", status=OrderStatus.SUCCESS, message="ok")
        sl_result = TradeResult(order_id="SL_VBL", status=OrderStatus.SUCCESS, message="ok")
        close_result = TradeResult(order_id="CLOSE_VBL", status=OrderStatus.SUCCESS, message="ok")

        sent_orders = []

        async def capture_send(order):
            sent_orders.append(order)
            if not sent_orders or len(sent_orders) == 1:
                return entry_result
            return close_result

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=15_000.0),
            patch("signal_engine.main.build_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, side_effect=[entry_result, close_result]),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock,
                  return_value=(sl_result, MagicMock())),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.main.fetch_order_fill_price", new_callable=AsyncMock,
                  return_value=533.95),  # fill above TP 532.75
            patch("signal_engine.main.tracker") as mock_tracker,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.settings") as mock_settings,
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 15_000.0
            mock_risk.calculate_quantity.return_value = 22
            mock_settings.bracket_enabled = True
            mock_settings.bracket_cnc_sl_enabled = False
            mock_settings.risk_per_trade = 0.015
            mock_settings.slippage_factor = 0.10
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0
            mock_settings.mis_margin_pct = 0.20
            mock_settings.max_sl_pct_for_sizing = 0.0

            await handle_message("ORB LONG\nSymbol: VBL\nEntry: 528\nSL: 519.34\nTP: 532.75")

        # SL must be cancelled before close
        mock_cancel.assert_awaited_once_with("SL_VBL", mock_signal.strategy)
        # Tracker must NOT have a new position registered
        mock_tracker.register.assert_not_called()
        # risk_engine.record_close must be called to free the slot
        mock_risk.record_close.assert_called_once_with(0.0, symbol="VBL")

    @pytest.mark.asyncio
    async def test_short_fill_below_tp_auto_closes(self):
        """SHORT fill below TP → same auto-close path triggered."""
        mock_signal = self._mock_signal(
            direction=Direction.SHORT, entry=413.5, sl=421.91, tp=408.75
        )
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        entry_result = TradeResult(order_id="E_SHORT", status=OrderStatus.SUCCESS, message="ok")
        sl_result = TradeResult(order_id="SL_SHORT", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=15_000.0),
            patch("signal_engine.main.build_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock,
                  return_value=TradeResult(order_id="E_SHORT", status=OrderStatus.SUCCESS, message="ok")),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock,
                  return_value=(sl_result, MagicMock())),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock),
            patch("signal_engine.main.fetch_order_fill_price", new_callable=AsyncMock,
                  return_value=407.0),  # fill below TP 408.75 for SHORT
            patch("signal_engine.main.tracker") as mock_tracker,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.settings") as mock_settings,
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 15_000.0
            mock_risk.calculate_quantity.return_value = 23
            mock_settings.bracket_enabled = True
            mock_settings.bracket_cnc_sl_enabled = False
            mock_settings.risk_per_trade = 0.015
            mock_settings.slippage_factor = 0.10
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0
            mock_settings.mis_margin_pct = 0.20
            mock_settings.max_sl_pct_for_sizing = 0.0

            await handle_message("ORB SHORT\nSymbol: POONAWALLA\nEntry: 413.5\nSL: 421.91\nTP: 408.75")

        mock_tracker.register.assert_not_called()
        mock_risk.record_close.assert_called_once_with(0.0, symbol="VBL")

    @pytest.mark.asyncio
    async def test_normal_fill_within_tp_registers_in_tracker(self):
        """Fill within TP range (normal slippage) must proceed to tracker registration."""
        mock_signal = self._mock_signal()
        valid_result = ValidationResult(status=ValidationStatus.VALID)
        entry_result = TradeResult(order_id="E_NORM", status=OrderStatus.SUCCESS, message="ok")
        sl_result = TradeResult(order_id="SL_NORM", status=OrderStatus.SUCCESS, message="ok")

        with (
            patch("signal_engine.main.parse", return_value=mock_signal),
            patch("signal_engine.main.validate", return_value=valid_result),
            patch("signal_engine.main.risk_engine") as mock_risk,
            patch("signal_engine.main.fetch_available_capital", new_callable=AsyncMock, return_value=15_000.0),
            patch("signal_engine.main.build_order", return_value=MagicMock()),
            patch("signal_engine.main.send_order", new_callable=AsyncMock, return_value=entry_result),
            patch("signal_engine.main.send_bracket_legs", new_callable=AsyncMock,
                  return_value=(sl_result, MagicMock())),
            patch("signal_engine.main.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.main.fetch_order_fill_price", new_callable=AsyncMock,
                  return_value=529.5),  # fill within TP 532.75 — normal
            patch("signal_engine.main.tracker") as mock_tracker,
            patch("signal_engine.main.save"),
            patch("signal_engine.main.settings") as mock_settings,
            patch("signal_engine.main.notifier", new_callable=AsyncMock),
        ):
            mock_risk.check_exposure.return_value = True
            mock_risk.get_sizing_capital.return_value = 15_000.0
            mock_risk.calculate_quantity.return_value = 22
            mock_settings.bracket_enabled = True
            mock_settings.bracket_cnc_sl_enabled = False
            mock_settings.risk_per_trade = 0.015
            mock_settings.slippage_factor = 0.10
            mock_settings.sizing_mode = "fixed_fractional"
            mock_settings.exchange = "NSE"
            mock_settings.product = "MIS"
            mock_settings.test_qty_cap = 0
            mock_settings.min_capital_for_entry = 0
            mock_settings.mis_margin_pct = 0.20
            mock_settings.max_sl_pct_for_sizing = 0.0

            await handle_message("ORB LONG\nSymbol: VBL\nEntry: 528\nSL: 519.34\nTP: 532.75")

        mock_cancel.assert_not_called()       # SL must NOT be cancelled
        mock_tracker.register.assert_called_once()  # must register normally
