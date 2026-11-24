"""E-07: a server error or a post-send transport error is not a rejection.

OpenAlgo core answers HTTP 500 when its broker call raised, which can happen after the broker
accepted the order. Classing that as REJECTED made the exit loop resend it. Only a failure that
proves the request was never sent (4xx, connect error) is a definite failure.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from signal_engine import order_reconcile as oc
from signal_engine.executor import send_order
from signal_engine.main import _send_exit_with_retries
from signal_engine.models import Action, Direction, Order, OrderStatus

ORDER = Order(symbol="SBIN", exchange="NSE", action=Action.SELL, quantity=10, price=0.0,
              order_type="MARKET", product="MIS", strategy_tag="T")


def _client(*, response=None, error=None):
    client = AsyncMock()
    client.post = AsyncMock(return_value=response, side_effect=error)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _response(status, body=None, text="boom"):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = body if body is not None else {"status": "error", "message": text}
    return resp


async def _send(**kw):
    with patch("httpx.AsyncClient", return_value=_client(**kw)):
        return await send_order(ORDER)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [500, 502, 504])
async def test_server_error_is_outcome_unknown(code):
    result = await _send(response=_response(code, text="broker call failed"))
    assert result.status == OrderStatus.TIMEOUT
    assert result.message == "broker call failed"  # the server's own text, no status code
    assert str(code) not in result.message


@pytest.mark.asyncio
async def test_server_error_without_a_message_gets_a_trader_sentence():
    resp = _response(500, body={})
    result = await _send(response=resp)
    assert result.status == OrderStatus.TIMEOUT
    assert result.message == "the server did not confirm whether the order was placed"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 403, 429])
async def test_client_error_is_a_rejection(code):
    result = await _send(response=_response(code, text="refused"))
    assert result.status == OrderStatus.REJECTED
    assert result.message == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc", [httpx.ReadError("reset"), httpx.WriteError("w"), httpx.RemoteProtocolError("closed")]
)
async def test_transport_error_after_send_is_outcome_unknown(exc):
    result = await _send(error=exc)
    assert result.status == OrderStatus.TIMEOUT
    assert result.message == "the server did not confirm whether the order was placed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc", [httpx.ReadError("reset"), httpx.RemoteProtocolError("closed"), httpx.WriteError("w")]
)
async def test_no_timeout_message_carries_exception_text(exc):
    result = await _send(error=exc)
    assert result.status == OrderStatus.TIMEOUT
    assert "Error(" not in result.message and "Exception" not in result.message
    assert "reset" not in result.message and "closed" not in result.message


@pytest.mark.asyncio
async def test_bad_json_timeout_message_carries_no_exception_text():
    resp = _response(200)
    resp.json.side_effect = ValueError("not json")
    result = await _send(response=resp)
    assert result.status == OrderStatus.TIMEOUT
    assert "Error(" not in result.message and "Exception" not in result.message
    assert "not json" not in result.message


@pytest.mark.asyncio
async def test_connect_error_is_a_definite_failure():
    result = await _send(error=httpx.ConnectError("refused"))
    assert result.status == OrderStatus.ERROR


@pytest.mark.asyncio
async def test_connect_timeout_stays_timeout():
    assert (await _send(error=httpx.ConnectTimeout("t"))).status == OrderStatus.TIMEOUT


@pytest.mark.asyncio
async def test_bad_json_on_a_200_reply_is_outcome_unknown():
    resp = _response(200)
    resp.json.side_effect = ValueError("not json")
    result = await _send(response=resp)
    assert result.status == OrderStatus.TIMEOUT


@pytest.mark.asyncio
async def test_error_before_the_request_is_sent_stays_error():
    client = _client()
    with patch("httpx.AsyncClient", side_effect=RuntimeError("no client")):
        result = await send_order(ORDER)
    assert result.status == OrderStatus.ERROR
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_that_gets_500_is_not_resent_while_the_book_cannot_prove_it_absent():
    oc._uncertain.clear()
    pos = SimpleNamespace(symbol="SBIN", strategy="T", quantity=10, exchange="NSE",
                          product="MIS", direction=Direction.LONG)
    settings = SimpleNamespace(bracket_tp_exit_retries=3, bracket_retry_delay=0.0,
                               entry_timeout_recovery_attempts=3,
                               entry_timeout_recovery_delay_seconds=0.0)
    post = AsyncMock(return_value=_response(500, text="broker call failed"))
    client = _client()
    client.post = post
    try:
        with (
            patch("httpx.AsyncClient", return_value=client),
            patch("signal_engine.main.settings", settings),
            patch("signal_engine.main.fetch_orderbook", AsyncMock(return_value=None)),
            patch("signal_engine.main.tracker",
                  MagicMock(fetch_own_position=AsyncMock(return_value=None))),
            patch("signal_engine.order_reconcile.asyncio", SimpleNamespace(sleep=AsyncMock()))
        ):
            result = await _send_exit_with_retries(ORDER, pos)
        assert post.await_count == 1
        assert result.status == OrderStatus.TIMEOUT
    finally:
        oc._uncertain.clear()
