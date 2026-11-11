"""E-01: an order pipeline must not be cancellable from outside.

Telethon's disconnect() cancels every event-handler task. The handler used to await the
pipeline inline, so a Telegram drop delivered CancelledError into send_order, the bracket or
the exit path. The pipeline now runs as its own shielded task, tracked in a set that shutdown
drains before it stops the tracker.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from loguru import logger

from signal_engine import freshness, listener
from signal_engine.listener import _make_handler


class _Msg:
    def __init__(self, text="ORB LONG\nSymbol: SBIN", date=None):
        self.text = text
        self.date = date


class _Event:
    def __init__(self, chat_id=-1, text="ORB LONG\nSymbol: SBIN", date=None):
        self.chat_id = chat_id
        self.message = _Msg(text, date)


@pytest.fixture(autouse=True)
def _clean_inflight():
    getattr(listener, "_inflight_dispatches", set()).clear()
    yield
    getattr(listener, "_inflight_dispatches", set()).clear()


@pytest.fixture
def log_lines():
    lines = []
    sink_id = logger.add(lambda m: lines.append(m.record["level"].name + " " + m.record["message"]))
    yield lines
    logger.remove(sink_id)


def _handler(on_message):
    # "smidestn" has no phase suffix, so no OpenAlgo mode lookup happens in the handler.
    return _make_handler(on_message, {-1: "smidestn"})


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_handler_cancelled_mid_pipeline_still_completes_the_pipeline():
    gate = asyncio.Event()
    started = asyncio.Event()
    done = []

    async def on_message(text):
        started.set()
        await gate.wait()  # stands in for send_order awaiting the broker
        done.append("saved_and_registered")

    with patch("signal_engine.listener._is_stale", return_value=None):
        handler_task = asyncio.create_task(_handler(on_message)(_Event()))
        await started.wait()
        handler_task.cancel()  # what Telethon disconnect() does
        with pytest.raises(asyncio.CancelledError):
            await handler_task
        assert done == []  # still suspended, not cancelled
        gate.set()
        await asyncio.sleep(0.05)
        assert done == ["saved_and_registered"]
        assert await listener.drain_inflight(5) == 0


@pytest.mark.asyncio
async def test_origin_time_is_visible_inside_the_shielded_task():
    seen = []

    async def on_message(text):
        seen.append(freshness.current_origin())

    sent = datetime(2026, 11, 10, 4, 0, 0)
    with patch("signal_engine.listener._is_stale", return_value=None):
        await _handler(on_message)(_Event(date=sent))
    assert seen == [sent.replace(tzinfo=UTC)]
    assert freshness.current_origin() is None  # the scope did not leak out


@pytest.mark.asyncio
async def test_exception_in_the_pipeline_is_logged_and_does_not_crash_the_handler(log_lines):
    async def on_message(text):
        raise ValueError("pipeline blew up")

    with patch("signal_engine.listener._is_stale", return_value=None):
        await _handler(on_message)(_Event())  # must not raise
        await _settle()
    assert any(line.startswith("ERROR") and "pipeline blew up" in line for line in log_lines)
    assert listener._inflight_dispatches == set()


@pytest.mark.asyncio
async def test_drain_returns_zero_when_nothing_is_running():
    assert await listener.drain_inflight(1) == 0


@pytest.mark.asyncio
async def test_drain_returns_zero_after_completion():
    async def work():
        await asyncio.sleep(0.01)

    task = listener.track_dispatch(work(), "unit")
    assert await listener.drain_inflight(2) == 0
    assert task.done()


@pytest.mark.asyncio
async def test_drain_reports_and_logs_a_task_that_outlives_the_timeout(log_lines):
    gate = asyncio.Event()

    async def work():
        await gate.wait()

    task = listener.track_dispatch(work(), "unit-slow")
    assert await listener.drain_inflight(0.05) == 1
    critical = [line for line in log_lines if line.startswith("CRITICAL")]
    assert critical and "unit-slow" in critical[0]
    assert "reconcile" in critical[0].lower()
    gate.set()
    await task


@pytest.mark.asyncio
async def test_runner_cancelled_during_a_suspended_submit_still_completes_the_dispatch():
    from signal_engine.pipeline import service

    gate = asyncio.Event()
    started = asyncio.Event()
    done = []

    async def dispatch(signal):
        started.set()
        await gate.wait()
        done.append(signal)

    cfg = SimpleNamespace(
        strategies={"T": SimpleNamespace(signal_source="python", paper_only=False)}
    )
    sink = service.build_sink(cfg, "T", dispatch)
    signal = SimpleNamespace(
        strategy="T", direction=SimpleNamespace(value="LONG"), symbol="SBIN", sig_id="x", tp_level=0
    )
    runner_task = asyncio.create_task(sink.submit(signal))  # the runner's per-strategy task
    await started.wait()
    runner_task.cancel()  # runner_service.cancel() at shutdown
    with pytest.raises(asyncio.CancelledError):
        await runner_task
    assert done == []
    gate.set()
    await asyncio.sleep(0.05)
    assert done == [signal]
    assert await listener.drain_inflight(5) == 0


@pytest.mark.asyncio
async def test_drain_also_waits_for_a_pipeline_started_during_the_drain():
    late = []

    async def late_work():
        await asyncio.sleep(0.1)

    async def first():
        await asyncio.sleep(0.05)
        late.append(listener.track_dispatch(late_work(), "unit-late"))

    listener.track_dispatch(first(), "unit-first")
    assert await listener.drain_inflight(2) == 0
    assert late and late[0].done(), "the drain returned while a late pipeline was still running"


@pytest.mark.asyncio
async def test_drain_uses_one_overall_deadline_across_rounds(log_lines):
    gate = asyncio.Event()

    async def stuck():
        await gate.wait()

    async def first():
        await asyncio.sleep(0.05)
        listener.track_dispatch(stuck(), "unit-stuck-late")

    listener.track_dispatch(first(), "unit-first2")
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await listener.drain_inflight(0.3) == 1
    assert loop.time() - started < 1.0
    assert any(line.startswith("CRITICAL") and "unit-stuck-late" in line for line in log_lines)
    gate.set()
    await asyncio.sleep(0.05)
