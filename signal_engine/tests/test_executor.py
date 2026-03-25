"""Tests for order executor — RED phase first."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from signal_engine.executor import build_order, build_exit_order, build_sl_order, build_tp_order, send_bracket_legs, send_order
from signal_engine.models import Action, Direction, OrderStatus, Signal
from signal_engine.tests.conftest import make_signal as _make_signal


class TestBuildOrder:
    def test_long_maps_to_buy(self):
        order = build_order(_make_signal(direction=Direction.LONG), quantity=10)
        assert order.action == Action.BUY

    def test_short_maps_to_sell(self):
        order = build_order(
            _make_signal(direction=Direction.SHORT, entry=3800, sl=3830, tp=3750),
            quantity=10,
        )
        assert order.action == Action.SELL

    def test_market_order_price_zero(self):
        order = build_order(_make_signal(), quantity=10)
        assert order.price == 0

    def test_limit_order_uses_entry_price(self):
        order = build_order(_make_signal(), quantity=10, order_type="LIMIT")
        assert order.price == 2500.0

    def test_quantity_set(self):
        order = build_order(_make_signal(), quantity=42)
        assert order.quantity == 42

    def test_strategy_tag_captured(self):
        order = build_order(_make_signal(strategy="VWAP"), quantity=10)
        assert order.strategy_tag == "VWAP"

    def test_exchange_and_product_defaults(self):
        order = build_order(_make_signal(), quantity=10)
        assert order.exchange == "NSE"
        assert order.product == "MIS"


class TestSendOrder:
    @pytest.mark.asyncio
    async def test_success_response(self):
        order = build_order(_make_signal(), quantity=10)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"orderid": "12345", "status": "success"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_order(order)
            assert result.status == OrderStatus.SUCCESS
            assert result.order_id == "12345"

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        order = build_order(_make_signal(), quantity=10)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_order(order)
            assert result.status == OrderStatus.TIMEOUT
            assert result.order_id == ""

    @pytest.mark.asyncio
    async def test_http_error_handling(self):
        order = build_order(_make_signal(), quantity=10)
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {
            "status": "error",
            "message": "MIS orders cannot be placed after square-off time",
            "mode": "analyze",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_order(order)
            assert result.status == OrderStatus.REJECTED
            assert "square-off" in result.message

    @pytest.mark.asyncio
    async def test_generic_error_handling(self):
        order = build_order(_make_signal(), quantity=10)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("connection failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await send_order(order)
            assert result.status == OrderStatus.ERROR


class TestBuildExitOrder:
    """Tests for build_exit_order — MARKET SELL to close an existing LONG position."""

    def test_action_is_sell(self):
        order = build_exit_order("RELIANCE", "NSE", 50, "CNC", "RSI-TP-MR")
        assert order.action == Action.SELL

    def test_order_type_is_market(self):
        order = build_exit_order("RELIANCE", "NSE", 50, "CNC", "RSI-TP-MR")
        assert order.order_type == "MARKET"

    def test_price_is_zero(self):
        order = build_exit_order("RELIANCE", "NSE", 50, "CNC", "RSI-TP-MR")
        assert order.price == 0.0

    def test_quantity_matches(self):
        order = build_exit_order("RELIANCE", "NSE", 42, "CNC", "RSI-TP-MR")
        assert order.quantity == 42

    def test_product_passthrough(self):
        order = build_exit_order("RELIANCE", "NSE", 50, "CNC", "RSI-TP-MR")
        assert order.product == "CNC"

    def test_strategy_tag_captured(self):
        order = build_exit_order("RELIANCE", "NSE", 50, "CNC", "RSI-TP-MR")
        assert order.strategy_tag == "RSI-TP-MR"

    def test_symbol_and_exchange(self):
        order = build_exit_order("HDFCBANK", "NSE", 10, "MIS", "ORB")
        assert order.symbol == "HDFCBANK"
        assert order.exchange == "NSE"


class TestBuildSlOrder:
    def test_long_entry_produces_sell_slm(self):
        signal = _make_signal(direction=Direction.LONG, entry=2500.0, sl=2485.0)
        order = build_sl_order(signal, quantity=10)
        assert order.action == Action.SELL

    def test_short_entry_produces_buy_slm(self):
        signal = _make_signal(direction=Direction.SHORT, entry=2500.0, sl=2515.0)
        order = build_sl_order(signal, quantity=10)
        assert order.action == Action.BUY

    def test_sl_order_type_is_slm(self):
        signal = _make_signal(direction=Direction.LONG, sl=2485.0)
        order = build_sl_order(signal, quantity=10)
        assert order.order_type == "SL-M"

    def test_trigger_price_set_to_sl(self):
        signal = _make_signal(direction=Direction.LONG, sl=2485.0)
        order = build_sl_order(signal, quantity=10)
        assert order.trigger_price == 2485.0

    def test_quantity_matches(self):
        signal = _make_signal(direction=Direction.LONG)
        order = build_sl_order(signal, quantity=42)
        assert order.quantity == 42

    def test_price_is_zero_for_slm(self):
        signal = _make_signal(direction=Direction.LONG, sl=2485.0)
        order = build_sl_order(signal, quantity=10)
        assert order.price == 0.0

    def test_symbol_and_strategy_captured(self):
        signal = _make_signal(symbol="TCS", strategy="ORB")
        order = build_sl_order(signal, quantity=5)
        assert order.symbol == "TCS"
        assert order.strategy_tag == "ORB"


class TestBuildTpOrder:
    def test_long_entry_produces_sell_limit(self):
        signal = _make_signal(direction=Direction.LONG, tp=2540.0)
        order = build_tp_order(signal, quantity=10)
        assert order.action == Action.SELL

    def test_short_entry_produces_buy_limit(self):
        signal = _make_signal(direction=Direction.SHORT, entry=2500.0, sl=2515.0, tp=2460.0)
        order = build_tp_order(signal, quantity=10)
        assert order.action == Action.BUY

    def test_tp_order_type_is_limit(self):
        signal = _make_signal(tp=2540.0)
        order = build_tp_order(signal, quantity=10)
        assert order.order_type == "LIMIT"

    def test_price_set_to_tp(self):
        signal = _make_signal(tp=2540.0)
        order = build_tp_order(signal, quantity=10)
        assert order.price == 2540.0

    def test_quantity_matches(self):
        signal = _make_signal()
        order = build_tp_order(signal, quantity=77)
        assert order.quantity == 77

    def test_trigger_price_is_zero(self):
        signal = _make_signal(tp=2540.0)
        order = build_tp_order(signal, quantity=10)
        assert order.trigger_price == 0.0

    def test_symbol_and_strategy_captured(self):
        signal = _make_signal(symbol="INFY", strategy="VWAP")
        order = build_tp_order(signal, quantity=5)
        assert order.symbol == "INFY"
        assert order.strategy_tag == "VWAP"


class TestSendBracketLegs:
    def _success_result(self, order_id="SL001"):
        return MagicMock(status=OrderStatus.SUCCESS, order_id=order_id)

    def _failure_result(self):
        return MagicMock(status=OrderStatus.REJECTED, order_id="")

    @pytest.mark.asyncio
    async def test_sl_placed_tp_always_none(self):
        """send_bracket_legs places SL only. TP is handled by the tracker via LTP monitoring."""
        signal = _make_signal()
        sl_result = self._success_result("SL001")

        with patch("signal_engine.executor.send_order", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = sl_result
            result_sl, result_tp = await send_bracket_legs(signal, quantity=10, entry_order_id="E001")

        assert result_sl.order_id == "SL001"
        assert result_tp is None
        assert mock_send.call_count == 1

    @pytest.mark.asyncio
    async def test_tp_not_placed_when_sl_fails(self):
        signal = _make_signal()
        failure = self._failure_result()

        with patch("signal_engine.executor.send_order", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = failure
            result_sl, result_tp = await send_bracket_legs(signal, quantity=10, entry_order_id="E001")

        assert result_sl.status == OrderStatus.REJECTED
        assert result_tp is None
        # Only the SL order attempts (retries), TP should never be called
        # All calls should be to build_sl_order (no TP attempt)

    @pytest.mark.asyncio
    async def test_sl_fails_all_retries_returns_failure(self):
        signal = _make_signal()
        failure = self._failure_result()

        with patch("signal_engine.executor.send_order", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = failure
            result_sl, result_tp = await send_bracket_legs(signal, quantity=10, entry_order_id="E001")

        # Should have retried bracket_max_retries times (3 by default from config)
        assert result_sl.status == OrderStatus.REJECTED
        assert result_tp is None

    @pytest.mark.asyncio
    async def test_sl_order_type_is_slm(self):
        """SL order must be placed as SL-M (not LIMIT)."""
        signal = _make_signal(direction=Direction.LONG, sl=2485.0, tp=2540.0)
        orders_placed = []

        async def record_order(order):
            orders_placed.append(order.order_type)
            return self._success_result(f"ID{len(orders_placed)}")

        with patch("signal_engine.executor.send_order", side_effect=record_order):
            await send_bracket_legs(signal, quantity=10, entry_order_id="E001")

        assert len(orders_placed) == 1
        assert orders_placed[0] == "SL-M"
