"""Releasing a feed subscription must not hold up the fill that released it.

``WebSocketExecutionEngine._unsubscribe_ws_symbols`` waits up to 10 s for the proxy to
acknowledge. It used to run inline in ``notify_position_closed`` and ``notify_order_completed``,
which are called from fill processing, so a slow acknowledgement stalled that thread. The
release is now queued to one worker. The worker re-checks the reference count before it
sends, so a symbol re-subscribed in the meantime keeps its subscription.
"""

from __future__ import annotations

import threading
import time

import pytest

from sandbox.websocket_execution_engine import WebSocketExecutionEngine

USER = "unsub_user"


@pytest.fixture
def engine(monkeypatch):
    eng = WebSocketExecutionEngine()
    eng._running = True
    sent: list[tuple[str, str]] = []
    gate = threading.Event()
    gate.set()
    entered = threading.Event()

    def fake_unsubscribe(user_id, symbols):
        entered.set()
        gate.wait(timeout=5)
        for symbol, exchange in symbols:
            sent.append((symbol, exchange))
        return True

    monkeypatch.setattr(eng, "_unsubscribe_ws_symbols", fake_unsubscribe)
    monkeypatch.setattr(eng, "_subscribe_ws_symbols", lambda user_id, symbols: None)
    eng.sent, eng.gate, eng.entered = sent, gate, entered
    yield eng
    eng.gate.set()
    eng._running = False
    eng._stop_unsubscribe_worker()


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _hold_position(eng, symbol="RELIANCE", exchange="NSE"):
    key = f"{exchange}:{symbol}"
    eng._position_refs.add((USER, key))
    eng._user_symbol_refcounts.setdefault(USER, {})[key] = 1


def test_position_release_returns_before_the_proxy_answers(engine):
    _hold_position(engine)
    engine.gate.clear()  # the proxy is slow to acknowledge

    started = time.perf_counter()
    engine.notify_position_closed(USER, "RELIANCE", "NSE")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert engine.entered.wait(2)  # the worker did start the release
    assert engine.sent == []
    engine.gate.set()
    assert _wait(lambda: engine.sent == [("RELIANCE", "NSE")])


def test_order_completion_release_does_not_block_either(engine):
    key = "NSE:SBIN"
    engine._pending_orders_index[key] = ["o1"]
    engine._user_symbol_refcounts[USER] = {key: 1}
    engine.gate.clear()

    started = time.perf_counter()
    engine.notify_order_completed("o1", key, USER)
    assert time.perf_counter() - started < 0.5
    engine.gate.set()
    assert _wait(lambda: engine.sent == [("SBIN", "NSE")])


def test_releases_are_sent_in_the_order_they_were_made(engine):
    for symbol in ("AAA", "BBB", "CCC"):
        _hold_position(engine, symbol)
    engine.gate.clear()
    for symbol in ("AAA", "BBB", "CCC"):
        engine.notify_position_closed(USER, symbol, "NSE")
    engine.gate.set()
    assert _wait(lambda: len(engine.sent) == 3)
    assert [s for s, _ in engine.sent] == ["AAA", "BBB", "CCC"]


def test_a_symbol_subscribed_again_before_its_release_is_sent_keeps_its_subscription(engine):
    _hold_position(engine, "AAA")
    _hold_position(engine, "BBB")
    engine.gate.clear()
    engine.notify_position_closed(USER, "AAA", "NSE")  # worker blocks on this one
    assert engine.entered.wait(2)
    engine.notify_position_closed(USER, "BBB", "NSE")  # queued behind it
    engine.notify_position_opened(USER, "BBB", "NSE")  # position reopens before it is sent
    engine.gate.set()

    assert _wait(lambda: ("AAA", "NSE") in engine.sent)
    assert _wait(lambda: engine.stats()["skipped_resubscribed"] == 1)
    assert ("BBB", "NSE") not in engine.sent
    assert engine._user_symbol_refcounts[USER]["NSE:BBB"] == 1


def test_a_failed_release_is_counted_logged_and_does_not_stop_the_worker(
    engine, caplog, monkeypatch
):
    def flaky(user_id, symbols):
        if symbols[0][0] == "AAA":
            return False
        engine.sent.append(symbols[0])
        return True

    monkeypatch.setattr(engine, "_unsubscribe_ws_symbols", flaky)
    _hold_position(engine, "AAA")
    _hold_position(engine, "BBB")
    with caplog.at_level("WARNING"):
        engine.notify_position_closed(USER, "AAA", "NSE")
        engine.notify_position_closed(USER, "BBB", "NSE")
        assert _wait(lambda: engine.sent == [("BBB", "NSE")])

    stats = engine.stats()
    assert stats["failed"] == 1 and stats["sent"] == 1
    assert any(
        "AAA" in r.getMessage() and "release" in r.getMessage().lower() for r in caplog.records
    )


def test_an_exception_in_a_release_is_survived(engine, monkeypatch):
    def boom(user_id, symbols):
        if symbols[0][0] == "AAA":
            raise RuntimeError("proxy gone")
        engine.sent.append(symbols[0])
        return True

    monkeypatch.setattr(engine, "_unsubscribe_ws_symbols", boom)
    _hold_position(engine, "AAA")
    _hold_position(engine, "BBB")
    engine.notify_position_closed(USER, "AAA", "NSE")
    engine.notify_position_closed(USER, "BBB", "NSE")
    assert _wait(lambda: engine.sent == [("BBB", "NSE")])
    assert engine.stats()["failed"] == 1


def test_the_time_a_release_takes_is_measured(engine):
    _hold_position(engine, "AAA")
    engine.gate.clear()
    engine.notify_position_closed(USER, "AAA", "NSE")
    assert engine.entered.wait(2)
    time.sleep(0.2)
    engine.gate.set()
    assert _wait(lambda: engine.stats()["sent"] == 1)
    assert engine.stats()["max_release_seconds"] >= 0.2


def test_stopping_the_engine_still_releases_everything_synchronously(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(
        engine, "_unsubscribe_ws_symbols", lambda u, s: calls.append((u, list(s))) or True
    )
    engine._user_symbol_refcounts = {USER: {"NSE:AAA": 1, "NSE:BBB": 2}}
    engine._unsubscribe_all_ws()
    released = sorted(sym for _, syms in calls for sym in syms)
    assert released == [("AAA", "NSE"), ("BBB", "NSE")]


def test_queued_releases_are_dropped_when_the_engine_stops(engine):
    _hold_position(engine, "AAA")
    _hold_position(engine, "BBB")
    engine.gate.clear()
    engine.notify_position_closed(USER, "AAA", "NSE")
    assert engine.entered.wait(2)
    engine.notify_position_closed(USER, "BBB", "NSE")
    engine._running = False  # stop(): _unsubscribe_all_ws() covers every held symbol
    engine.gate.set()
    assert _wait(lambda: ("AAA", "NSE") in engine.sent)
    time.sleep(0.2)
    assert ("BBB", "NSE") not in engine.sent


def test_the_fill_path_returns_while_the_proxy_is_still_silent(engine):
    """ExecutionEngine's fill hook is what calls the release; it must not wait for the proxy."""
    from types import SimpleNamespace

    from sandbox.execution_engine import ExecutionEngine

    _hold_position(engine, "RELIANCE")
    engine.gate.clear()
    order = SimpleNamespace(user_id=USER, symbol="RELIANCE", exchange="NSE")

    started = time.perf_counter()
    ExecutionEngine._apply_position_feed_change(order, engine, has_open=False)
    assert time.perf_counter() - started < 0.5
    assert engine.entered.wait(2)
    engine.gate.set()
    assert _wait(lambda: engine.sent == [("RELIANCE", "NSE")])


def test_open_close_open_close_leaves_the_symbol_released_exactly_once(engine, monkeypatch):
    subscribed = []
    monkeypatch.setattr(engine, "_subscribe_ws_symbols", lambda u, s: subscribed.append(s[0]))
    engine.notify_position_opened(USER, "AAA", "NSE")  # subscribe
    engine.notify_position_closed(USER, "AAA", "NSE")  # release queued
    engine.notify_position_opened(USER, "AAA", "NSE")  # subscribe again before it is sent
    engine.notify_position_closed(USER, "AAA", "NSE")  # final release
    assert _wait(lambda: engine.stats()["queued"] == 2)
    assert _wait(lambda: engine.stats()["sent"] + engine.stats()["skipped_resubscribed"] == 2)
    assert subscribed == [("AAA", "NSE"), ("AAA", "NSE")]
    assert engine.sent.count(("AAA", "NSE")) >= 1
    assert "NSE:AAA" not in engine._user_symbol_refcounts.get(USER, {})  # nothing left held


def test_the_worker_comes_back_after_the_engine_is_stopped_and_started_again(engine):
    _hold_position(engine, "AAA")
    engine.notify_position_closed(USER, "AAA", "NSE")
    assert _wait(lambda: engine.sent == [("AAA", "NSE")])

    engine._running = False
    engine._stop_unsubscribe_worker()
    assert _wait(
        lambda: engine._unsubscribe_thread is None or not engine._unsubscribe_thread.is_alive()
    )

    engine._running = True  # start() again
    _hold_position(engine, "BBB")
    engine.notify_position_closed(USER, "BBB", "NSE")
    assert _wait(lambda: ("BBB", "NSE") in engine.sent)


def test_a_release_queued_right_after_a_stop_and_start_is_not_stranded(engine):
    engine.gate.clear()
    _hold_position(engine, "AAA")
    engine.notify_position_closed(USER, "AAA", "NSE")  # worker is busy with this one
    assert engine.entered.wait(2)

    engine._running = False
    engine._stop_unsubscribe_worker()  # stop marker queued behind the busy worker
    engine._running = True  # started again at once
    _hold_position(engine, "BBB")
    engine.notify_position_closed(USER, "BBB", "NSE")  # queued behind the stop marker
    engine.gate.set()

    assert _wait(lambda: ("BBB", "NSE") in engine.sent)
