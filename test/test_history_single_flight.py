"""Concurrent identical broker-history requests share one broker call.

A caller that times out leaves its request running on the server, and its retry used to start
a second one behind it. Requests for the same symbol, interval and date range now join the one
already in flight. The shared result is the broker's own answer; nothing is cached once the
flight ends, so a later request always asks the broker again.
"""

from __future__ import annotations

import threading
import time

import pytest

from services import history_service

OK = (True, {"status": "success", "data": [{"close": 1.0}]}, 200)


@pytest.fixture
def broker(monkeypatch):
    """A controllable broker call plus counters for the broker and the 3-per-second spacer."""
    state = {"calls": 0, "spacer": 0, "gate": threading.Event(), "result": OK, "raises": None}
    state["gate"].set()

    def fake_with_auth(auth_token, feed_token, broker_name, symbol, exchange, *rest):
        state["calls"] += 1
        state["gate"].wait(timeout=5)
        if state["raises"]:
            raise state["raises"]
        return state["result"]

    monkeypatch.setattr(history_service, "get_history_with_auth", fake_with_auth)
    monkeypatch.setattr(
        history_service,
        "_enforce_rate_limit",
        lambda **kw: state.__setitem__("spacer", state["spacer"] + 1),
    )
    monkeypatch.setattr(
        history_service,
        "get_auth_token_broker",
        lambda key, include_feed_token=False: ("tok", None, "somebroker"),
    )
    # A broker that does not pace its own history, so the spacer counter is meaningful.
    monkeypatch.setattr(history_service, "import_broker_module", lambda name: None)
    yield state
    state["gate"].set()


def _request(symbol="TCS", interval="5m", day="2026-10-28", api_key="k"):
    return history_service.get_history(symbol, "NSE", interval, day, day, api_key=api_key)


def _run_together(n, **kw):
    out, threads = [], []
    lock = threading.Lock()

    def work():
        r = _request(**kw)
        with lock:
            out.append(r)

    for _ in range(n):
        t = threading.Thread(target=work)
        threads.append(t)
        t.start()
    return out, threads


def _join(threads):
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()


def _wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_concurrent_identical_requests_make_one_broker_call_and_one_spacer_slot(broker):
    broker["gate"].clear()
    out, threads = _run_together(6)
    assert _wait_for(lambda: broker["calls"] == 1)
    time.sleep(0.1)  # let the followers arrive and join
    broker["gate"].set()
    _join(threads)
    assert broker["calls"] == 1
    assert broker["spacer"] == 1  # followers never book a spacer slot
    assert len(out) == 6 and all(r == OK for r in out)


def test_requests_that_differ_in_any_part_of_the_key_do_not_share(broker):
    broker["gate"].clear()
    variants = [
        {"symbol": "TCS"},
        {"symbol": "INFY"},
        {"interval": "1m"},
        {"day": "2026-10-27"},
        {"api_key": "other"},
    ]
    results, threads = [], []
    for v in variants:
        t = threading.Thread(target=lambda v=v: results.append(_request(**v)))
        threads.append(t)
        t.start()
    assert _wait_for(lambda: broker["calls"] == len(variants))
    broker["gate"].set()
    _join(threads)
    assert broker["calls"] == len(variants)


def test_a_retry_while_the_first_request_is_still_running_joins_it(broker):
    broker["gate"].clear()
    first, t1 = _run_together(1)
    assert _wait_for(lambda: broker["calls"] == 1)
    retry, t2 = _run_together(1)  # the client timed out and asked again
    time.sleep(0.1)
    assert broker["calls"] == 1 and broker["spacer"] == 1
    broker["gate"].set()
    _join(t1 + t2)
    assert first == retry == [OK]


def test_each_caller_gets_its_own_copy_of_the_result(broker):
    broker["result"] = (True, {"status": "success", "data": [{"close": 1.0}]}, 200)
    original = broker["result"]
    broker["gate"].clear()
    out, threads = _run_together(4)
    assert _wait_for(lambda: broker["calls"] == 1)
    time.sleep(0.1)
    broker["gate"].set()
    _join(threads)
    # Whichever thread led got the broker's own object; the other three got private copies.
    copies = [r for r in out if r is not original]
    assert len(copies) == 3
    copies[0][1]["data"][0]["close"] = 999.0
    assert copies[1][1]["data"][0]["close"] == 1.0
    assert copies[2][1]["data"][0]["close"] == 1.0
    assert original[1]["data"][0]["close"] == 1.0


def test_a_failure_is_shared_with_the_waiters_and_not_kept(broker):
    failure = (False, {"status": "error", "message": "broker said no"}, 500)
    broker["result"] = failure
    broker["gate"].clear()
    out, threads = _run_together(4)
    assert _wait_for(lambda: broker["calls"] == 1)
    time.sleep(0.1)
    broker["gate"].set()
    _join(threads)
    assert broker["calls"] == 1 and all(r == failure for r in out)

    broker["result"] = OK  # the next request is a fresh flight, not the remembered failure
    assert _request() == OK
    assert broker["calls"] == 2


def test_an_exception_in_the_broker_call_reaches_the_waiters_and_cleans_up(broker):
    broker["raises"] = RuntimeError("socket closed")
    broker["gate"].clear()
    errors, threads = [], []

    def work():
        try:
            _request()
        except RuntimeError as exc:
            errors.append(str(exc))

    for _ in range(3):
        t = threading.Thread(target=work)
        threads.append(t)
        t.start()
    assert _wait_for(lambda: broker["calls"] == 1)
    time.sleep(0.1)
    broker["gate"].set()
    _join(threads)
    assert errors == ["socket closed"] * 3
    assert history_service._inflight == {}

    broker["raises"] = None
    assert _request() == OK


def test_the_registry_is_empty_after_every_flight(broker):
    for symbol in ("A", "B", "C"):
        _request(symbol=symbol)
    assert history_service._inflight == {}


def test_a_waiter_that_loses_patience_fetches_for_itself(broker, monkeypatch):
    monkeypatch.setattr(history_service, "_FLIGHT_WAIT_TIMEOUT", 0.2)
    broker["gate"].clear()
    _, t1 = _run_together(1)
    assert _wait_for(lambda: broker["calls"] == 1)
    started = time.time()
    follower, t2 = _run_together(1)
    assert _wait_for(lambda: broker["calls"] == 2, timeout=2)  # it gave up and asked itself
    assert time.time() - started < 2
    broker["gate"].set()
    _join(t1 + t2)
    assert follower == [OK]


def test_a_flight_older_than_the_join_window_is_not_joined(broker, monkeypatch):
    monkeypatch.setattr(history_service, "_FLIGHT_JOIN_WINDOW", 0.15)
    broker["gate"].clear()
    _, t1 = _run_together(1)
    assert _wait_for(lambda: broker["calls"] == 1)
    time.sleep(0.3)  # the first request is now too old to stand in for a new one
    _, t2 = _run_together(1)
    assert _wait_for(lambda: broker["calls"] == 2)
    broker["gate"].set()
    _join(t1 + t2)
    assert history_service._inflight == {}


def test_a_late_finishing_old_flight_does_not_remove_its_replacement(broker, monkeypatch):
    monkeypatch.setattr(history_service, "_FLIGHT_JOIN_WINDOW", 0.15)
    broker["gate"].clear()
    _, t1 = _run_together(1)
    assert _wait_for(lambda: broker["calls"] == 1)
    time.sleep(0.3)
    _, t2 = _run_together(1)
    assert _wait_for(lambda: broker["calls"] == 2)
    assert len(history_service._inflight) == 1
    broker["gate"].set()
    _join(t1 + t2)
    assert history_service._inflight == {}


def test_database_history_is_not_single_flighted(monkeypatch):
    calls = []
    monkeypatch.setattr(history_service, "get_history_from_db", lambda **kw: calls.append(kw) or OK)
    for _ in range(3):
        history_service.get_history("TCS", "NSE", "5m", "2026-10-28", "2026-10-28", source="db")
    assert len(calls) == 3


def test_invalid_parameters_still_return_the_same_error(broker):
    ok, body, status = history_service.get_history("TCS", "NSE", "5m", "2026-10-28", "2026-10-28")
    assert (ok, status) == (False, 400)
    assert "api_key or both auth_token and broker" in body["message"]
    assert broker["calls"] == 0


def test_internal_callers_with_a_token_share_too(broker):
    broker["gate"].clear()
    out, threads = [], []
    for _ in range(3):
        t = threading.Thread(
            target=lambda: out.append(
                history_service.get_history(
                    "TCS", "NSE", "5m", "d", "d", auth_token="tok", broker="flattrade"
                )
            )
        )
        threads.append(t)
        t.start()
    assert _wait_for(lambda: broker["calls"] == 1)
    time.sleep(0.1)
    broker["gate"].set()
    _join(threads)
    assert broker["calls"] == 1 and len(out) == 3
