"""Tests for order executor — RED phase first."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from signal_engine.executor import build_order, send_order
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
