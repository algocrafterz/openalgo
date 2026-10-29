"""A pooled connection that Flattrade closed must cost one retry, not a failed read request."""

import httpx
import pytest

from broker.flattrade.api import data


class _Reply:
    text = '{"stat": "Ok"}'


class _Client:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def request(self, *a, **k):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _Reply()


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(data, "_apply_rate_limit", lambda: None)
    monkeypatch.setenv("BROKER_API_KEY", "uid:::secret")

    def install(failures):
        client = _Client(failures)
        monkeypatch.setattr(data, "get_httpx_client", lambda: client)
        return client

    return install


@pytest.mark.parametrize(
    "error",
    [
        httpx.RemoteProtocolError("Server disconnected"),
        httpx.ReadError("EOF occurred in violation of protocol"),
    ],
)
def test_a_dropped_connection_is_retried_once(patched, error):
    client = patched([error])
    assert data.get_api_response("/PiConnectAPI/GetQuotes", "tok") == {"stat": "Ok"}
    assert client.calls == 2


def test_a_second_drop_is_raised(patched):
    client = patched([httpx.RemoteProtocolError("a"), httpx.RemoteProtocolError("b")])
    with pytest.raises(httpx.RemoteProtocolError):
        data.get_api_response("/PiConnectAPI/GetQuotes", "tok")
    assert client.calls == 2


def test_a_timeout_is_not_retried(patched):
    client = patched([httpx.ReadTimeout("slow")])
    with pytest.raises(httpx.ReadTimeout):
        data.get_api_response("/PiConnectAPI/GetQuotes", "tok")
    assert client.calls == 1
