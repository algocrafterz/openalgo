"""Tests for OpenAlgo API client."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from signal_engine.api_client import (
    cancel_order,
    fetch_available_capital,
    fetch_open_position,
    fetch_order_status,
    fetch_realised_pnl,
    fetch_trading_mode,
)


def _mock_client(response_data, status_code=200):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = response_data
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestFetchAvailableCapital:
    @pytest.mark.asyncio
    async def test_success(self):
        data = {"status": "success", "data": {"availablecash": "250000.50"}}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_available_capital()
            assert result == 250000.50

    @pytest.mark.asyncio
    async def test_non_success_status(self):
        data = {"status": "error", "message": "Invalid API key"}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_available_capital()
            assert result == 0.0

    @pytest.mark.asyncio
    async def test_network_error(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await fetch_available_capital()
            assert result == 0.0

    @pytest.mark.asyncio
    async def test_missing_field_returns_zero(self):
        data = {"status": "success", "data": {}}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_available_capital()
            assert result == 0.0


class TestFetchOpenPosition:
    @pytest.mark.asyncio
    async def test_position_open(self):
        data = {"status": "success", "quantity": 50}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_open_position("RELIANCE", "ORB", "NSE", "MIS")
            assert result == 50

    @pytest.mark.asyncio
    async def test_position_closed(self):
        data = {"status": "success", "quantity": 0}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_open_position("RELIANCE", "ORB", "NSE", "MIS")
            assert result == 0

    @pytest.mark.asyncio
    async def test_api_error_returns_negative_one(self):
        data = {"status": "error"}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_open_position("RELIANCE", "ORB", "NSE", "MIS")
            assert result == -1

    @pytest.mark.asyncio
    async def test_network_error_returns_negative_one(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await fetch_open_position("RELIANCE", "ORB", "NSE", "MIS")
            assert result == -1


class TestFetchRealisedPnl:
    @pytest.mark.asyncio
    async def test_success(self):
        data = {"status": "success", "data": {"m2mrealized": "1500.75"}}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_realised_pnl()
            assert result == 1500.75

    @pytest.mark.asyncio
    async def test_error_returns_zero(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await fetch_realised_pnl()
            assert result == 0.0


class TestFetchTradingMode:
    @pytest.mark.asyncio
    async def test_live_mode(self):
        data = {"status": "success", "data": {"mode": "live", "analyze_mode": False}}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            mode_str, is_analyze = await fetch_trading_mode()
            assert mode_str == "live"
            assert is_analyze is False

    @pytest.mark.asyncio
    async def test_analyze_mode(self):
        data = {"status": "success", "data": {"mode": "analyze", "analyze_mode": True}}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            mode_str, is_analyze = await fetch_trading_mode()
            assert mode_str == "analyze"
            assert is_analyze is True

    @pytest.mark.asyncio
    async def test_error_returns_unknown(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            mode_str, is_analyze = await fetch_trading_mode()
            assert mode_str == "unknown"
            assert is_analyze is False


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        data = {"status": "success", "orderid": "ORD123"}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await cancel_order("ORD123", "ORB")
            assert result is True

    @pytest.mark.asyncio
    async def test_error_status_returns_false(self):
        data = {"status": "error", "message": "Order not found"}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await cancel_order("ORD999", "ORB")
            assert result is False

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self):
        data = {"status": "error", "message": "bad request"}
        with patch("httpx.AsyncClient", return_value=_mock_client(data, status_code=400)):
            result = await cancel_order("ORD123", "ORB")
            assert result is False

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await cancel_order("ORD123", "ORB")
            assert result is False


class TestFetchOrderStatus:
    @pytest.mark.asyncio
    async def test_success_returns_status_string(self):
        data = {"status": "success", "data": {"orderstatus": "complete"}}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_order_status("ORD123", "ORB")
            assert result == "complete"

    @pytest.mark.asyncio
    async def test_api_error_returns_empty_string(self):
        data = {"status": "error", "message": "Order not found"}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_order_status("ORD999", "ORB")
            assert result == ""

    @pytest.mark.asyncio
    async def test_network_error_returns_empty_string(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("fail"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await fetch_order_status("ORD123", "ORB")
            assert result == ""

    @pytest.mark.asyncio
    async def test_missing_orderstatus_field_returns_empty(self):
        data = {"status": "success", "data": {}}
        with patch("httpx.AsyncClient", return_value=_mock_client(data)):
            result = await fetch_order_status("ORD123", "ORB")
            assert result == ""
