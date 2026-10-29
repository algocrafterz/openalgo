"""Identical concurrent multi-quote requests share one broker fetch.

Flattrade has no batch quote call, so a multi-quote of N symbols costs N broker requests. Four
identical 25-symbol requests inside a few seconds (2026-10-29 13:39 IST) booked about 100 requests
against a 110 per minute cap and queued orders for up to 49 seconds.
"""

import threading
import time

import pytest

from services import quotes_service
from utils import broker_priority
from utils.single_flight import SingleFlight

SYMBOLS = [{"symbol": "SBIN", "exchange": "NSE"}, {"symbol": "INFY", "exchange": "NSE"}]
OTHER = [{"symbol": "TCS", "exchange": "NSE"}]


class _Handler:
    """Broker data handler that counts calls and can hold the first one open."""

    calls = 0
    release = None
    started = None

    def __init__(self, auth_token):
        self.auth_token = auth_token

    def get_multiquotes(self, symbols):
        type(self).calls += 1
        if type(self).started is not None:
            type(self).started.set()
        if type(self).release is not None:
            type(self).release.wait(5)
        return [
            {"symbol": s["symbol"], "exchange": s["exchange"], "data": {"ltp": 1.0}}
            for s in symbols
        ]


class _Module:
    BrokerData = _Handler


@pytest.fixture(autouse=True)
def stubbed(monkeypatch):
    _Handler.calls = 0
    _Handler.release = threading.Event()
    _Handler.started = threading.Event()
    monkeypatch.setattr(quotes_service, "import_broker_module", lambda broker: _Module)
    monkeypatch.setattr(
        quotes_service,
        "validate_symbols_bulk",
        lambda symbols: (True, [{**s, "valid": True} for s in symbols], None),
    )
    quotes_service._flights.clear()
    yield
    _Handler.release.set()


def _call(results, index, symbols, token="tok"):
    results[index] = quotes_service.get_multiquotes_with_auth(token, None, "flattrade", symbols)


def _run_concurrently(requests):
    results = [None] * len(requests)
    threads = []
    for i, (symbols, token) in enumerate(requests):
        t = threading.Thread(target=_call, args=(results, i, symbols, token))
        t.start()
        threads.append(t)
        if i == 0:
            assert _Handler.started.wait(2)
        time.sleep(0.05)
    _Handler.release.set()
    for t in threads:
        t.join(5)
    return results


def test_duplicate_requests_share_one_broker_fetch():
    results = _run_concurrently([(SYMBOLS, "tok")] * 4)
    assert _Handler.calls == 1
    assert all(r == results[0] for r in results)
    assert results[0][0] is True and results[0][2] == 200
    assert [r["symbol"] for r in results[0][1]["results"]] == ["SBIN", "INFY"]


def test_distinct_symbol_lists_stay_independent():
    _run_concurrently([(SYMBOLS, "tok"), (OTHER, "tok")])
    assert _Handler.calls == 2


def test_distinct_accounts_stay_independent():
    _run_concurrently([(SYMBOLS, "tokA"), (SYMBOLS, "tokB")])
    assert _Handler.calls == 2


def test_distinct_priority_lanes_stay_independent():
    results = [None, None]

    def background():
        with broker_priority.background():
            _call(results, 1, SYMBOLS)

    first = threading.Thread(target=_call, args=(results, 0, SYMBOLS))
    first.start()
    assert _Handler.started.wait(2)
    second = threading.Thread(target=background)
    second.start()
    time.sleep(0.1)
    _Handler.release.set()
    first.join(5)
    second.join(5)
    assert _Handler.calls == 2


def test_nothing_is_kept_after_the_flight_ends():
    _Handler.release.set()
    quotes_service.get_multiquotes_with_auth("tok", None, "flattrade", SYMBOLS)
    quotes_service.get_multiquotes_with_auth("tok", None, "flattrade", SYMBOLS)
    assert _Handler.calls == 2


def test_joiners_get_independent_copies():
    results = _run_concurrently([(SYMBOLS, "tok")] * 2)
    results[0][1]["results"].append("mutated")
    assert "mutated" not in results[1][1]["results"]


def test_a_failure_reaches_every_joiner_with_the_same_response(monkeypatch):
    def boom(self, symbols):
        type(self).calls += 1
        type(self).started.set()
        type(self).release.wait(5)
        raise RuntimeError("upstream down")

    monkeypatch.setattr(_Handler, "get_multiquotes", boom)
    results = _run_concurrently([(SYMBOLS, "tok")] * 3)
    assert _Handler.calls == 1
    assert all(r[0] is False and r[2] == 500 for r in results)


def test_a_stale_flight_is_not_joined():
    flights = SingleFlight(join_window=0.0, wait_timeout=5)
    calls = []
    release = threading.Event()

    def slow():
        calls.append(1)
        release.wait(5)
        return "x"

    t = threading.Thread(target=lambda: flights.do(("k",), slow))
    t.start()
    time.sleep(0.05)
    out = flights.do(("k",), lambda: (calls.append(2), "y")[1])
    release.set()
    t.join(5)
    assert out == "y"
    assert calls == [1, 2]


def test_a_waiter_that_times_out_fetches_for_itself():
    flights = SingleFlight(join_window=30, wait_timeout=0.1)
    release = threading.Event()
    t = threading.Thread(target=lambda: flights.do(("k",), lambda: release.wait(5)))
    t.start()
    time.sleep(0.05)
    assert flights.do(("k",), lambda: "own") == "own"
    release.set()
    t.join(5)


def test_counters_report_joined_requests():
    flights = SingleFlight(join_window=30, wait_timeout=5)
    release = threading.Event()
    threads = [
        threading.Thread(target=lambda: flights.do(("k",), lambda: release.wait(5)))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
        time.sleep(0.03)
    release.set()
    for t in threads:
        t.join(5)
    assert flights.stats() == {"led": 1, "joined": 3}
