"""placeorder has its own timeout (BUG-007): a sandbox burst queues orders for up to ~13 s."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import executor
from signal_engine.config import settings
from signal_engine.models import Action, Order


def test_the_configured_order_timeout_covers_the_measured_burst():
    assert settings.order_timeout == 20.0  # 2026-10-30 10:15: the sixth order took 12.7 s
    assert settings.api_timeout == 5.0  # every other call is unchanged


@pytest.mark.asyncio
async def test_send_order_uses_the_order_timeout_not_the_api_timeout():
    order = Order(
        symbol="TCS",
        exchange="NSE",
        action=Action.BUY,
        quantity=1,
        price=0.0,
        order_type="MARKET",
        product="MIS",
        strategy_tag="ORB",
    )
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=executor.httpx.TimeoutException("slow"))
    with patch.object(executor.httpx, "AsyncClient", return_value=client) as made:
        result = await executor.send_order(order)
    assert made.call_args.kwargs["timeout"] == settings.order_timeout
    assert result.status.name == "TIMEOUT"
