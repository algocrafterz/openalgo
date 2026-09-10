"""fetch_orderbook() — the call that makes a restart's SL recovery possible (H5)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import api_client


def _mock_client(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestFetchOrderbook:
    @pytest.mark.asyncio
    async def test_returns_a_bare_list_payload(self):
        orders = [{"orderid": "1", "symbol": "SBIN"}]
        with patch("httpx.AsyncClient", return_value=_mock_client({"status": "success", "data": orders})):
            assert await api_client.fetch_orderbook() == orders

    @pytest.mark.asyncio
    async def test_unwraps_the_orders_key_when_the_broker_nests_it(self):
        orders = [{"orderid": "1", "symbol": "SBIN"}]
        payload = {"status": "success", "data": {"orders": orders, "statistics": {}}}
        with patch("httpx.AsyncClient", return_value=_mock_client(payload)):
            assert await api_client.fetch_orderbook() == orders

    @pytest.mark.asyncio
    async def test_non_success_status_returns_none(self):
        with patch("httpx.AsyncClient", return_value=_mock_client({"status": "error"})):
            assert await api_client.fetch_orderbook() is None

    @pytest.mark.asyncio
    async def test_retries_then_gives_up_returning_none(self):
        client = MagicMock()
        client.post = AsyncMock(side_effect=RuntimeError("connection refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=client), \
             patch("signal_engine.api_client.asyncio.sleep", AsyncMock()):
            assert await api_client.fetch_orderbook() is None
        assert client.post.await_count == 3
