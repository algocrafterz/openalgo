"""G8a: the message of an order that was never sent is for a trader, not the exception text.

The stop and exit failure paths log TradeResult.message, so it must read as plain trader text.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from signal_engine.executor import build_order, send_order
from signal_engine.models import OrderStatus
from signal_engine.tests.test_executor import _make_signal

_EXPECTED = "could not reach OpenAlgo, so the order was not sent"


async def _send(error):
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=error)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=mock_client):
        return await send_order(build_order(_make_signal(), quantity=10))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("[Errno 111] Connection refused 127.0.0.1:5000"), ConnectionError("secret detail")],
)
async def test_an_order_that_was_never_sent_carries_no_exception_text(error):
    result = await _send(error)
    assert result.status == OrderStatus.ERROR
    assert result.message == _EXPECTED
