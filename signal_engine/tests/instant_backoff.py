"""Opt-in fixture that removes the real waiting from api_client retry backoffs.

The tests that import this fixture reach api_client without mocking its HTTP layer; the
autouse guard in conftest.py makes every such call raise, so the real retry loops run
(fetch_order_fill_price sleeps 1 s between attempts, fetch_orderbook 2 s, the margin
call settings.bracket_retry_delay). The loops are the code under test elsewhere
(test_api_client.py, test_api_client_orderbook.py, test_degraded_dependency_visibility.py)
and here they still run in full: only the wait is removed. Each requested delay is
recorded and the fake still yields to the event loop once, like a real sleep does.

Only the name `asyncio` inside signal_engine.api_client is replaced, by a proxy that
forwards everything except `sleep`. The real asyncio.sleep stays untouched for the
test's own coordination code and for other modules.

Use:
    from signal_engine.tests.instant_backoff import instant_backoff  # noqa: F401
    pytestmark = pytest.mark.usefixtures("instant_backoff")
"""

import asyncio

import pytest

_real_sleep = asyncio.sleep


class _AsyncioProxy:
    """The asyncio module, with sleep() replaced by a recording no-wait version."""

    def __init__(self, recorded: list[float]):
        self._recorded = recorded

    async def sleep(self, delay, *args, **kwargs):
        self._recorded.append(delay)
        await _real_sleep(0)

    def __getattr__(self, name):
        return getattr(asyncio, name)


@pytest.fixture
def instant_backoff(monkeypatch):
    """Make api_client's retry sleeps instant; yield the list of requested delays."""
    recorded: list[float] = []
    monkeypatch.setattr("signal_engine.api_client.asyncio", _AsyncioProxy(recorded))
    return recorded
