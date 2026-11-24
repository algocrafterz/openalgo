"""E-10: the tracker poll loop must not die silently.

check_positions() ran in a bare loop with no try/except and the task had no done-callback, so
one exception ended close detection, stop retry and the outage alert while the process and the
heartbeat looked healthy.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import startup
from signal_engine import tracker as tracker_module
from signal_engine.strategies import ORB, RSI_TP_MR
from signal_engine.tests.log_capture import capturing
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker


def _poll_events(logs) -> list[str]:
    """The supervision events logged, in order: 'Tracker poll alert (<event>): <text>' lines.

    Reads INFO and above, so a recovery (INFO) is seen next to a failure (ERROR).
    """
    out = []
    for msg in logs.messages("INFO"):
        if msg.startswith("Tracker poll alert ("):
            out.append(msg[len("Tracker poll alert (") :].split(")", 1)[0])
    return out


def _tracker(interval=0.01) -> PositionTracker:
    t = PositionTracker(_make_engine(), poll_interval=interval)
    t.register(_make_position())
    t._load_realised_baseline = AsyncMock()
    return t


async def _run_for(t: PositionTracker, seconds: float) -> asyncio.Task:
    task = asyncio.create_task(t.start())
    await asyncio.sleep(seconds)
    return task


async def _stop(t: PositionTracker, task: asyncio.Task) -> None:
    t.stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_one_exception_does_not_end_the_loop_and_logs_once():
    t = _tracker()
    calls = []

    async def check():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("positionbook parse failed")

    t.check_positions = check
    with capturing() as logs:
        task = await _run_for(t, 0.2)
        assert not task.done(), "the poll task ended after one exception"
        await _stop(t, task)
    assert len(calls) > 2  # kept polling
    alerts = [
        m
        for m in logs.messages("ERROR")
        if m.startswith("Tracker poll alert (tracker_poll_failed)")
    ]
    assert len(alerts) == 1
    assert "being retried" in alerts[0]
    assert "RuntimeError" not in alerts[0]  # trader text, no exception class


@pytest.mark.asyncio
async def test_repeated_failure_log_lines_are_rate_limited():
    t = _tracker()
    t.check_positions = AsyncMock(side_effect=RuntimeError("down"))
    with capturing() as logs:
        task = await _run_for(t, 0.2)
        assert t.check_positions.await_count > 5
        assert _poll_events(logs) == ["tracker_poll_failed"]  # first failure only, inside 5 minutes
        # Pretend the last line was logged 6 minutes ago: the next failure is logged again.
        t._poll_last_alert_at = time.monotonic() - 360
        await asyncio.sleep(0.1)
        await _stop(t, task)
    assert _poll_events(logs) == ["tracker_poll_failed", "tracker_poll_failed"]


@pytest.mark.asyncio
async def test_recovery_logs_one_line_and_records_liveness():
    t = _tracker()
    state = {"fail": True}

    async def check():
        if state["fail"]:
            raise RuntimeError("down")

    t.check_positions = check
    assert t.last_poll_ok_at is None
    with capturing() as logs:
        task = await _run_for(t, 0.1)
        assert t.last_poll_ok_at is None  # never succeeded yet
        state["fail"] = False
        await asyncio.sleep(0.1)
        await _stop(t, task)
    assert _poll_events(logs) == ["tracker_poll_failed", "tracker_poll_recovered"]
    assert t.last_poll_ok_at is not None
    assert t.last_poll_ok_at.utcoffset().total_seconds() == 5.5 * 3600  # IST


@pytest.mark.asyncio
async def test_idle_iteration_with_no_positions_counts_as_ok():
    t = PositionTracker(_make_engine(), poll_interval=0.01)
    t._load_realised_baseline = AsyncMock()
    task = await _run_for(t, 0.05)
    await _stop(t, task)
    assert t.last_poll_ok_at is not None


@pytest.mark.asyncio
async def test_repeated_failures_never_stall_the_loop():
    t = _tracker()
    t.check_positions = AsyncMock(side_effect=RuntimeError("down"))

    # Reporting a failed poll is now one log line, so it cannot wait on anything: the loop keeps
    # polling at its own interval (this once guarded a stalled outgoing message).
    with capturing() as logs:
        task = await _run_for(t, 0.4)
        assert not task.done()
        assert t.check_positions.await_count >= 2
        await _stop(t, task)
    assert _poll_events(logs) == ["tracker_poll_failed"]


@pytest.mark.asyncio
async def test_cancelled_error_still_ends_the_loop():
    t = _tracker(interval=60)
    t.check_positions = AsyncMock(side_effect=asyncio.CancelledError)
    task = asyncio.create_task(t.start())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_one_position_raising_does_not_skip_the_next():
    t = PositionTracker(_make_engine(), poll_interval=60)
    t.register(_make_position(symbol="AAA", strategy=ORB))
    t.register(_make_position(symbol="BBB", strategy=RSI_TP_MR))
    seen = []

    async def one(key, pos, book_data, _settings):
        seen.append(pos.symbol)
        if pos.symbol == "AAA":
            raise ValueError("bad row")
        return False

    t._check_one_position = one
    with (
        patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
        patch.object(tracker_module, "fetch_positionbook", AsyncMock(return_value=[{}])),
        patch.object(tracker_module, "_index_positionbook", MagicMock(return_value={})),
        patch.object(t, "_reconcile_stops", AsyncMock()),
        patch.object(t, "_note_positionbook_success", AsyncMock()),
        patch.object(t, "_mark_stale_prices", MagicMock(return_value={})),
        patch.object(t, "_push_unrealised", MagicMock()),
        patch.object(t, "_check_mode_flip", AsyncMock()),
        patch.object(t, "_maybe_send_day_summary", AsyncMock()) as summary,
        patch.object(t, "_retry_missing_sl", AsyncMock()) as retry_sl,
        patch.object(t, "_check_no_progress", AsyncMock()),
    ):
        with pytest.raises(RuntimeError):  # the poll counts as failed for the alerting
            await t.check_positions()
    assert seen == ["AAA", "BBB"]
    summary.assert_awaited_once()  # the rest of the poll still ran
    retry_sl.assert_awaited_once()


class TestDoneCallback:
    @staticmethod
    async def _ended(coro_fn, stopping=False):
        stop = asyncio.Event()
        if stopping:
            stop.set()
        task = asyncio.create_task(coro_fn())
        with capturing() as logs:
            task.add_done_callback(startup._background_task_ended("Position tracker", "TEXT", stop))
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
            await asyncio.sleep(0.05)
        return logs

    @pytest.mark.asyncio
    async def test_unexpected_normal_end_is_logged_critical(self):
        async def ends():
            return None

        logs = await self._ended(ends)
        lines = logs.messages("CRITICAL")
        assert len(lines) == 1
        assert lines[0] == "Position tracker task ended while the engine is still running. TEXT"

    @pytest.mark.asyncio
    async def test_exception_is_logged_critical_even_while_stopping(self):
        async def boom():
            raise RuntimeError("x")

        logs = await self._ended(boom, stopping=True)
        lines = logs.messages("CRITICAL")
        assert len(lines) == 1
        assert lines[0].startswith("Position tracker task stopped with an error: x. TEXT")

    @pytest.mark.asyncio
    async def test_end_during_shutdown_is_silent(self):
        async def ends():
            return None

        logs = await self._ended(ends, stopping=True)
        assert logs.messages("CRITICAL") == []

    @pytest.mark.asyncio
    async def test_cancellation_is_silent(self):
        async def hangs():
            await asyncio.sleep(60)

        stop = asyncio.Event()
        task = asyncio.create_task(hangs())
        with capturing() as logs:
            task.add_done_callback(startup._background_task_ended("Position tracker", "TEXT", stop))
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.05)
        assert logs.messages("CRITICAL") == []


@pytest.mark.asyncio
async def test_recovery_is_logged_at_info_and_failure_at_error():
    t = _tracker()
    state = {"fail": True}

    async def check():
        if state["fail"]:
            raise RuntimeError("down")

    t.check_positions = check
    with capturing() as logs:
        task = await _run_for(t, 0.1)
        state["fail"] = False
        await asyncio.sleep(0.1)
        await _stop(t, task)
    by_event = {
        event: name
        for no, name, msg in logs
        for event in ("tracker_poll_failed", "tracker_poll_recovered")
        if msg.startswith(f"Tracker poll alert ({event})")
    }
    assert by_event == {"tracker_poll_failed": "ERROR", "tracker_poll_recovered": "INFO"}
    # A recovery is not an error line.
    assert not any("tracker_poll_recovered" in m for m in logs.messages("ERROR"))


def test_log_poll_alert_defaults_to_error_and_honours_the_level():
    with capturing() as logs:
        PositionTracker._log_poll_alert("e1", "failed")
        PositionTracker._log_poll_alert("e2", "fine", level="INFO")
    assert [(name, msg) for _no, name, msg in logs] == [
        ("ERROR", "Tracker poll alert (e1): failed"),
        ("INFO", "Tracker poll alert (e2): fine"),
    ]
