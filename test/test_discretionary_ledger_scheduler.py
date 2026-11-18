"""Discretionary ledger scheduler: single-flight, failure isolation, window skip.

No broker or DB is touched: the sync function is replaced by a stub.
Run with: uv run pytest test/test_discretionary_ledger_scheduler.py -q
"""

import threading
import time
from datetime import datetime

import pytest

from services import discretionary_ledger_scheduler as sch


@pytest.fixture(autouse=True)
def clean():
    sch.stop_scheduler()
    sch._reset_state_for_tests()
    yield
    sch.stop_scheduler()
    sch._reset_state_for_tests()


def wait_idle(timeout=5):
    end = threading.Event()
    for _ in range(int(timeout * 100)):
        with sch._lock:
            if not sch._inflight:
                return True
        end.wait(0.01)
    return False


def test_single_flight_drops_overlapping_triggers():
    gate, started, calls = threading.Event(), threading.Event(), []

    def slow():
        calls.append(1)
        started.set()
        gate.wait(5)

    sch._sync_fn = slow
    assert sch.trigger_sync("manual") is True
    assert started.wait(5)
    assert sch.trigger_sync("manual") is False
    assert sch.trigger_sync("periodic") is False
    gate.set()
    assert wait_idle()
    assert sch.trigger_sync("manual") is True
    assert wait_idle()
    assert len(calls) == 2


def test_failure_is_contained_and_flight_flag_released():
    def boom():
        raise RuntimeError("broker exploded")

    sch._sync_fn = boom
    assert sch.trigger_sync("manual") is True  # does not raise
    assert wait_idle()
    ok = []
    sch._sync_fn = lambda: ok.append(1)
    assert sch.trigger_sync("manual") is True
    assert wait_idle() and ok == [1]


def test_one_named_worker_thread_reused():
    names = set()
    sch._sync_fn = lambda: names.add(threading.current_thread().name)
    for _ in range(3):
        assert sch.trigger_sync("manual")
        assert wait_idle()
    assert len(names) == 1 and next(iter(names)).startswith("discretionary-ledger")


def test_due_reason_market_hours_eod_and_window():
    def d(h, m, day=17):  # 17 Nov 2026 is a Tuesday
        return datetime(2026, 11, day, h, m)

    assert sch.due_reason(d(10, 0), None, None) == "periodic"
    assert sch.due_reason(d(10, 5), d(10, 0), None) is None  # inside the interval
    assert sch.due_reason(d(10, 15), d(10, 0), None) == "periodic"
    assert sch.due_reason(d(8, 0), None, None) is None  # before open
    assert sch.due_reason(d(15, 45), d(15, 30), None) is None  # post-close still running
    assert sch.due_reason(d(16, 14), d(15, 30), None) is None
    assert sch.due_reason(d(16, 15), d(15, 30), None) == "eod"
    assert sch.due_reason(d(16, 20), None, d(16, 15).date()) is None  # eod already done
    assert sch.due_reason(d(5, 30), None, None) is None  # token-clear window
    assert sch.due_reason(d(10, 0, day=21), None, None) is None  # Saturday


def test_tick_skips_window_and_runs_when_due():
    calls = []
    sch._sync_fn = lambda: calls.append(1)
    sch._now_fn = lambda: datetime(2026, 11, 17, 5, 30)
    sch._tick()
    assert wait_idle() and calls == []
    sch._now_fn = lambda: datetime(2026, 11, 17, 10, 0)
    sch._tick()
    assert wait_idle() and calls == [1]
    sch._tick()  # same minute: interval not elapsed
    assert wait_idle() and calls == [1]


def test_tick_exception_never_kills_the_ticker(monkeypatch):
    seen = []

    def bad_tick():
        seen.append(1)
        raise RuntimeError("clock")

    monkeypatch.setattr(sch, "_tick", bad_tick)
    monkeypatch.setattr(sch, "TICK_SECONDS", 0.01)
    sch._stop.clear()
    t = threading.Thread(target=sch._run_ticker, daemon=True)
    t.start()
    for _ in range(300):
        if len(seen) >= 3:
            break
        threading.Event().wait(0.01)
    assert len(seen) >= 3 and t.is_alive()
    sch._stop.set()
    t.join(5)
    assert not t.is_alive()


def test_start_is_idempotent_runs_boot_catchup_and_stops_cleanly():
    calls = []
    sch._sync_fn = lambda: calls.append(1)
    before = threading.active_count()
    sch.start_scheduler()
    sch.start_scheduler()
    assert wait_idle()
    assert calls == [1]  # one boot catch-up, no second ticker
    sch.stop_scheduler()
    for _ in range(300):  # shutdown(wait=False): the idle worker exits shortly after
        if threading.active_count() <= before:
            break
        threading.Event().wait(0.01)
    assert threading.active_count() <= before


def test_stop_scheduler_when_idle_is_safe_and_repeatable():
    sch.stop_scheduler()
    sch.stop_scheduler()
    assert sch._ticker is None and sch._executor is None


def test_stop_scheduler_after_start_leaves_no_ticker_thread():
    sch._sync_fn = lambda: None
    sch.start_scheduler()
    assert wait_idle()
    ticker = sch._ticker
    sch.stop_scheduler()
    assert not ticker.is_alive()
    assert sch._ticker is None and sch._executor is None


def test_stop_scheduler_does_not_block_on_a_running_sync():
    gate, started = threading.Event(), threading.Event()
    done = []

    def slow():
        started.set()
        gate.wait(10)
        done.append(1)

    sch._sync_fn = slow
    assert sch.trigger_sync("manual") is True
    assert started.wait(5)
    t0 = time.monotonic()
    sch.stop_scheduler()
    assert time.monotonic() - t0 < 2  # shutdown(wait=False): no join on the worker
    assert done == []  # the sync is still running
    gate.set()
    assert wait_idle()  # it finishes on its own and releases the flag
    assert done == [1]


def test_stop_scheduler_then_start_again_works():
    calls = []
    sch._sync_fn = lambda: calls.append(1)
    sch.start_scheduler()
    assert wait_idle()
    sch.stop_scheduler()
    sch.start_scheduler()
    assert wait_idle()
    assert calls == [1, 1]


def test_stop_scheduler_is_registered_with_atexit_once(monkeypatch):
    registered = []
    monkeypatch.setattr(sch.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(sch, "_atexit_registered", False)
    sch._sync_fn = lambda: None
    sch.start_scheduler()
    sch.stop_scheduler()
    sch.start_scheduler()
    assert registered == [sch.stop_scheduler]


def test_cancelled_queued_sync_does_not_leave_the_flight_flag_set():
    from concurrent.futures import Future

    fut = Future()
    assert fut.cancel()
    sch._inflight = True
    sch._future = fut
    sch._executor = sch.ThreadPoolExecutor(max_workers=1)
    sch.stop_scheduler()
    assert sch._inflight is False


def test_eod_time_default_and_env_override(monkeypatch):
    monkeypatch.delenv("DISCRETIONARY_LEDGER_EOD_TIME", raising=False)
    assert sch._eod_time() == (16, 15)
    monkeypatch.setenv("DISCRETIONARY_LEDGER_EOD_TIME", "17:05")
    assert sch._eod_time() == (17, 5)
    for bad in ("", "25:00", "16:61", "1615", "ab:cd", "16:15:00"):
        monkeypatch.setenv("DISCRETIONARY_LEDGER_EOD_TIME", bad)
        assert sch._eod_time() == (16, 15)
    monkeypatch.setenv("DISCRETIONARY_LEDGER_EOD_TIME", "17:05")
    t = datetime(2026, 11, 17, 16, 15)  # Tuesday
    assert sch.due_reason(t, None, None) is None
    assert sch.due_reason(t.replace(hour=17, minute=5), None, None) == "eod"


def test_eod_runs_once_per_day_and_late_start_still_runs(monkeypatch):
    monkeypatch.delenv("DISCRETIONARY_LEDGER_EOD_TIME", raising=False)
    calls = []
    sch._sync_fn = lambda: calls.append(1)
    sch._now_fn = lambda: datetime(2026, 11, 17, 16, 15)
    sch._tick()
    assert wait_idle() and calls == [1]
    sch._tick()
    sch._now_fn = lambda: datetime(2026, 11, 17, 16, 40)
    sch._tick()
    assert wait_idle() and calls == [1]  # not again the same day
    sch._now_fn = lambda: datetime(2026, 11, 18, 21, 0)  # restarted late on the next day
    sch._tick()
    assert wait_idle() and calls == [1, 1]


def test_eod_is_dropped_while_a_sync_is_in_flight_and_retried(monkeypatch):
    monkeypatch.delenv("DISCRETIONARY_LEDGER_EOD_TIME", raising=False)
    gate = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        gate.wait(5)

    sch._sync_fn = slow
    assert sch.trigger_sync("manual")
    sch._now_fn = lambda: datetime(2026, 11, 17, 16, 15)
    sch._tick()  # dropped: one sync in flight, eod not marked done
    assert sch._last_eod_date is None
    gate.set()
    assert wait_idle()
    sch._tick()  # next tick runs it
    assert wait_idle() and calls == [1, 1] and sch._last_eod_date is not None


def test_eod_time_change_does_not_touch_window_or_periodic(monkeypatch):
    monkeypatch.setenv("DISCRETIONARY_LEDGER_EOD_TIME", "16:15")
    d = lambda h, m: datetime(2026, 11, 17, h, m)  # noqa: E731
    assert sch.due_reason(d(15, 30), None, None) == "periodic"  # window end still inclusive
    assert sch.due_reason(d(15, 31), None, None) is None
    assert sch.due_reason(d(5, 30), None, None) is None
    assert sch.MARKET_OPEN == (9, 15) and sch.MARKET_CLOSE == (15, 30)
    from broker.flattrade.mapping.order_data import SYNC_SKIP_WINDOW

    assert SYNC_SKIP_WINDOW == ((5, 0), (6, 0))


# ------------------------------------------------------------------ end-of-day retry

OK = [{"status": "ok"}, {"status": "partial"}]
BAD = [{"status": "ok"}, {"status": "broker_error"}]


@pytest.fixture
def eod(monkeypatch):
    monkeypatch.delenv("DISCRETIONARY_LEDGER_EOD_TIME", raising=False)
    monkeypatch.delenv("DISCRETIONARY_LEDGER_RETRY_MIN", raising=False)
    state = {"now": datetime(2026, 11, 17, 16, 15), "results": [], "calls": 0}

    def fn():
        state["calls"] += 1
        return state["results"].pop(0) if state["results"] else OK

    sch._sync_fn = fn
    sch._now_fn = lambda: state["now"]

    def tick(h, m, day=17):
        state["now"] = datetime(2026, 11, day, h, m)
        sch._tick()
        assert wait_idle()

    state["tick"] = tick
    return state


def test_cycle_succeeded_rules():
    assert sch.cycle_succeeded(OK) and sch.cycle_succeeded([]) and sch.cycle_succeeded(None)
    for status in ("no_auth", "broker_error", "error", "skipped_window", "no_fill_data"):
        assert not sch.cycle_succeeded([{"status": "ok"}, {"status": status}])
    assert not sch.cycle_succeeded([{"bad": 1}])


def test_eod_runs_once_after_a_successful_cycle(eod):
    eod["results"] = [OK]
    for hm in ((16, 15), (16, 16), (16, 40), (23, 59)):
        eod["tick"](*hm)
    assert eod["calls"] == 1


def test_failed_cycle_is_retried_after_the_retry_interval(eod):
    eod["results"] = [BAD, OK]
    eod["tick"](16, 15)
    assert eod["calls"] == 1 and sch._last_eod_date is None
    eod["tick"](16, 30)
    assert eod["calls"] == 2 and sch._last_eod_date == datetime(2026, 11, 17).date()


def test_no_retry_inside_the_interval(eod):
    eod["results"] = [BAD, BAD, BAD]
    eod["tick"](16, 15)
    eod["tick"](16, 16)
    eod["tick"](16, 29)
    assert eod["calls"] == 1
    eod["tick"](16, 30)
    assert eod["calls"] == 2
    eod["tick"](16, 44)
    assert eod["calls"] == 2


def test_retry_interval_env_is_honoured(eod, monkeypatch):
    monkeypatch.setenv("DISCRETIONARY_LEDGER_RETRY_MIN", "2")
    eod["results"] = [BAD, OK]
    eod["tick"](16, 15)
    eod["tick"](16, 16)
    assert eod["calls"] == 1
    eod["tick"](16, 17)
    assert eod["calls"] == 2


def test_retries_stop_after_success(eod):
    eod["results"] = [BAD, BAD, OK]
    for hm in ((16, 15), (16, 30), (16, 45), (17, 15), (18, 0), (23, 0)):
        eod["tick"](*hm)
    assert eod["calls"] == 3


def test_exception_counts_as_failure_and_is_retried(eod):
    eod["results"] = [RuntimeError("x")]
    base = sch._sync_fn

    def fn():
        r = eod["results"]
        if r and isinstance(r[0], Exception):
            eod["calls"] += 1
            raise r.pop(0)
        return base()

    sch._sync_fn = fn
    eod["tick"](16, 15)
    assert eod["calls"] == 1 and sch._last_eod_date is None
    eod["tick"](16, 30)
    assert eod["calls"] == 2 and sch._last_eod_date is not None


def test_day_rollover_resets_the_eod_state(eod):
    eod["tick"](16, 15)
    assert eod["calls"] == 1 and sch._last_eod_date is not None
    eod["tick"](16, 15, day=18)  # next day (Wednesday)
    assert eod["calls"] == 2


def test_failed_day_does_not_carry_into_the_next_day(eod):
    eod["results"] = [BAD, BAD]
    eod["tick"](16, 15)
    eod["tick"](23, 59)
    assert eod["calls"] == 2
    eod["tick"](0, 5, day=18)  # before the eod time: nothing
    assert eod["calls"] == 2
    eod["tick"](16, 15, day=18)  # new day retried immediately (attempt was yesterday)
    assert eod["calls"] == 3


def test_skip_window_blocks_eod_and_retries(eod, monkeypatch):
    monkeypatch.setenv("DISCRETIONARY_LEDGER_EOD_TIME", "05:30")
    eod["tick"](5, 30)
    eod["tick"](5, 59)
    assert eod["calls"] == 0
    eod["tick"](6, 0)
    assert eod["calls"] == 1
    from broker.flattrade.mapping.order_data import SYNC_SKIP_WINDOW

    assert sch.SKIP_WINDOW == SYNC_SKIP_WINDOW


def test_overlapping_eod_is_dropped_without_burning_the_attempt(eod):
    gate = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        gate.wait(5)
        return OK

    sch._sync_fn = slow
    assert sch.trigger_sync("manual")
    eod["now"] = datetime(2026, 11, 17, 16, 15)
    sch._tick()  # dropped
    assert sch._last_eod_attempt is None and sch._last_eod_date is None
    gate.set()
    assert wait_idle()
    sch._tick()
    assert wait_idle() and len(calls) == 2 and sch._last_eod_date is not None


def test_periodic_success_never_marks_eod_done(eod):
    eod["tick"](10, 0)
    assert eod["calls"] == 1 and sch._last_eod_date is None
    eod["tick"](10, 15)
    assert eod["calls"] == 2  # periodic cadence unchanged
