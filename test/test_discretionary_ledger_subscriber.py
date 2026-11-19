"""Discretionary ledger subscriber: order.update -> one debounced ledger sync.

No broker call and no running app: the scheduler's sync function is a stub (or the real
sync_once with a mocked Flattrade TradeBook on temp SQLite in the latency test).
Run with: uv run pytest test/test_discretionary_ledger_subscriber.py -q
"""

import copy
import sqlite3
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from events import OrderUpdateEvent
from services import discretionary_ledger_scheduler as sch
from subscribers import discretionary_ledger_subscriber as sub

MARKET = datetime(2026, 11, 17, 11, 0, 0)  # Tuesday, inside the periodic window


class FakeTimer:
    """Deterministic stand-in for threading.Timer: tests fire it by hand."""

    created: list = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.cancelled, self.started = interval, fn, False, False
        self.daemon, self.name = False, ""
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert self.started and not self.cancelled
        self.fn()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    sch.stop_scheduler()
    sch._reset_state_for_tests()
    sub._reset_state_for_tests()
    FakeTimer.created = []
    monkeypatch.setenv("DISCRETIONARY_LEDGER_ENABLED", "true")
    monkeypatch.delenv("DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC", raising=False)
    monkeypatch.setattr(sch, "is_running", lambda: True)
    sch._now_fn = lambda: MARKET
    yield
    sub._reset_state_for_tests()
    sch.stop_scheduler()
    sch._reset_state_for_tests()


@pytest.fixture
def fake_timer(monkeypatch):
    monkeypatch.setattr(sub, "_timer_factory", FakeTimer)
    return FakeTimer


def ev(filled=10, mode="live", **kw):
    return OrderUpdateEvent(mode=mode, broker="flattrade", filled_quantity=filled, **kw)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def idle():
    with sch._lock:
        inflight = sch._inflight
    st = sub.get_stats()
    return not inflight and not st["sync_running"]


# ---------------------------------------------------------------- debounce and filters


def test_live_fill_triggers_exactly_one_sync_after_the_debounce(fake_timer):
    calls = []
    sch._sync_fn = lambda: calls.append(1) or [{"status": "ok"}]
    sub.on_order_update(ev())
    assert len(fake_timer.created) == 1 and fake_timer.created[0].interval == 8.0
    assert calls == []  # nothing before the window ends
    fake_timer.created[0].fire()
    assert wait_for(idle) and calls == [1]
    assert len(fake_timer.created) == 1  # no trailing re-check without a new event
    st = sub.get_stats()
    assert (st["events_seen"], st["syncs_triggered"], st["trailing"]) == (1, 1, 0)


def test_debounce_env_setting_is_honoured_and_clamped(fake_timer, monkeypatch):
    monkeypatch.setenv("DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC", "3")
    assert sub._debounce_seconds() == 3.0
    monkeypatch.setenv("DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC", "0")
    assert sub._debounce_seconds() == sub.DEBOUNCE_MIN_SEC
    monkeypatch.setenv("DISCRETIONARY_LEDGER_EVENT_DEBOUNCE_SEC", "junk")
    assert sub._debounce_seconds() == 8.0


def test_burst_of_ten_events_in_the_window_is_one_sync(fake_timer):
    calls = []
    sch._sync_fn = lambda: calls.append(1)
    for i in range(10):
        sub.on_order_update(ev(filled=i + 1))
    assert len(fake_timer.created) == 1
    fake_timer.created[0].fire()
    assert wait_for(idle) and calls == [1]
    st = sub.get_stats()
    assert (st["events_seen"], st["coalesced"], st["syncs_triggered"]) == (10, 9, 1)
    assert len(fake_timer.created) == 1


def test_events_during_a_running_sync_cause_exactly_one_trailing_sync(fake_timer):
    gate, started, calls = threading.Event(), threading.Event(), []

    def slow():
        calls.append(1)
        started.set()
        gate.wait(5)

    sch._sync_fn = slow
    sub.on_order_update(ev())
    fake_timer.created[0].fire()
    assert started.wait(5)
    for _ in range(5):  # a burst while the TradeBook is being read
        sub.on_order_update(ev(filled=20))
    assert len(fake_timer.created) == 1  # nothing armed while the sync runs
    gate.set()
    assert wait_for(lambda: len(fake_timer.created) == 2)  # one trailing re-check armed
    assert wait_for(idle)
    fake_timer.created[1].fire()
    assert wait_for(idle) and wait_for(lambda: len(calls) == 2)
    time.sleep(0.05)
    assert len(calls) == 2 and len(fake_timer.created) == 2  # no storm
    st = sub.get_stats()
    assert (st["syncs_triggered"], st["trailing"], st["coalesced"]) == (2, 1, 5)


def test_trigger_dropped_by_a_running_periodic_sync_is_retried_not_lost(fake_timer):
    gate, started, calls = threading.Event(), threading.Event(), []

    def slow():
        calls.append("sync")
        started.set()
        gate.wait(5)

    sch._sync_fn = slow
    assert sch.trigger_sync("periodic")
    assert started.wait(5)
    sub.on_order_update(ev())
    fake_timer.created[0].fire()  # scheduler busy: the trigger is dropped
    assert len(fake_timer.created) == 2 and sub.get_stats()["busy_retries"] == 1
    gate.set()
    assert wait_for(lambda: not sch._inflight)
    fake_timer.created[1].fire()
    assert wait_for(idle) and calls == ["sync", "sync"]
    assert sub.get_stats()["syncs_triggered"] == 1


@pytest.mark.parametrize("mode", ["analyze", "paper", "sandbox", ""])
def test_non_live_events_are_ignored(fake_timer, mode):
    sub.on_order_update(ev(mode=mode))
    assert fake_timer.created == [] and sub.get_stats()["events_ignored"] == 1


@pytest.mark.parametrize("filled", [0, -1, None, "x"])
def test_unfilled_events_are_ignored(fake_timer, filled):
    sub.on_order_update(SimpleNamespace(mode="live", filled_quantity=filled))
    assert fake_timer.created == []


def test_flag_off_schedules_nothing_and_never_imports_the_scheduler(fake_timer, monkeypatch):
    monkeypatch.setenv("DISCRETIONARY_LEDGER_ENABLED", "false")

    def boom():
        raise AssertionError("scheduler must not be touched with the flag off")

    monkeypatch.setattr(sub, "_scheduler", boom)
    sub.on_order_update(ev())
    assert fake_timer.created == [] and sub.get_stats()["errors"] == 0


def test_scheduler_not_started_schedules_nothing(fake_timer, monkeypatch):
    monkeypatch.setattr(sch, "is_running", lambda: False)
    sub.on_order_update(ev())
    assert fake_timer.created == []


def test_sync_failure_never_propagates_and_is_counted(fake_timer):
    def boom():
        raise RuntimeError("broker exploded")

    sch._sync_fn = boom
    sub.on_order_update(ev())
    fake_timer.created[0].fire()  # does not raise
    assert wait_for(idle)
    assert sub.get_stats()["failed"] == 1
    sch._sync_fn = lambda: [{"status": "broker_error"}]
    sub.on_order_update(ev())
    fake_timer.created[1].fire()
    assert wait_for(idle) and sub.get_stats()["failed"] == 2


def test_handler_errors_never_reach_the_event_bus(fake_timer, monkeypatch):
    def broken():
        raise RuntimeError("x")

    monkeypatch.setattr(sub, "_scheduler", broken)
    sub.on_order_update(ev())  # does not raise
    assert sub.get_stats()["errors"] == 1


def test_outside_the_periodic_window_nothing_is_queued(fake_timer):
    calls = []
    sch._sync_fn = lambda: calls.append(1)
    for now in (
        datetime(2026, 11, 17, 5, 30),  # 05:00-06:00 skip window
        datetime(2026, 11, 17, 9, 0),  # before the open
        datetime(2026, 11, 17, 15, 31),  # after 15:30: the end-of-day sync covers it
        datetime(2026, 11, 21, 11, 0),  # Saturday
    ):
        sch._now_fn = lambda now=now: now
        sub.on_order_update(ev())
        fake_timer.created[-1].fire()
    assert wait_for(idle) and calls == []
    assert sub.get_stats()["outside_window"] == 4


def test_event_sync_allowed_matches_the_periodic_window():
    ok = sch.event_sync_allowed
    assert ok(datetime(2026, 11, 17, 9, 15)) and ok(datetime(2026, 11, 17, 15, 30))
    assert not ok(datetime(2026, 11, 17, 9, 14)) and not ok(datetime(2026, 11, 17, 15, 31))
    assert not ok(datetime(2026, 11, 17, 5, 0)) and not ok(datetime(2026, 11, 22, 10, 0))


def test_registered_on_order_update_default_lane():
    from utils.event_bus import EventBus

    bus = EventBus()
    with patch("subscribers.bus", bus):
        import subscribers

        subscribers.register_all()
    subs = bus._subscribers["order.update"]
    mine = [s for s in subs if s.name == "discretionary_ledger:order_update"]
    assert len(mine) == 1 and mine[0].critical is False
    assert mine[0].callback is sub.on_order_update


# ---------------------------------------------------------------- real timers, TCS replay


def test_tcs_acceptance_sequence_replayed_with_real_timers(monkeypatch):
    """SELL 25 at t0, then partial BUY fills 10/20/25 of the cover order: one sync after the
    burst, then the late fill gets its own sync; the last sync starts after the last event."""
    monkeypatch.setattr(sub, "_debounce_seconds", lambda: 0.2)
    sync_starts = []

    def fake_sync():
        sync_starts.append(time.monotonic())
        time.sleep(0.1)  # a TradeBook read
        return [{"status": "ok"}]

    sch._sync_fn = fake_sync
    events = []

    def emit(**kw):
        events.append(time.monotonic())
        sub.on_order_update(ev(symbol="TCS", exchange="NSE", product="MIS", **kw))

    emit(action="SELL", quantity=25, filled=25, order_status="complete")  # t0
    time.sleep(0.05)
    emit(action="BUY", quantity=25, filled=10, order_status="open")
    time.sleep(0.05)
    emit(action="BUY", quantity=25, filled=20, order_status="open")
    assert wait_for(lambda: len(sync_starts) == 1)
    time.sleep(0.02)  # the 25 fill lands while the first sync reads the TradeBook
    emit(action="BUY", quantity=25, filled=25, order_status="complete")
    assert wait_for(lambda: len(sync_starts) == 2, timeout=3)
    assert wait_for(idle)
    time.sleep(0.4)  # no extra sync without a new event
    assert len(sync_starts) == 2
    assert sync_starts[-1] > events[-1]
    assert sync_starts[0] - events[0] >= 0.2  # the debounce window held


# ---------------------------------------------------------------- scheduler: periodic timer


def test_successful_event_sync_resets_the_periodic_timer():
    sch._sync_fn = lambda: [{"status": "ok"}, {"status": "partial"}]
    sch._last_periodic = datetime(2026, 11, 17, 10, 50)
    assert sch.due_reason(MARKET, sch._last_periodic, None) is None
    assert sch.trigger_sync(sch.EVENT_REASON) and wait_for(lambda: not sch._inflight)
    assert sch._last_periodic == MARKET
    interval = sch._interval_minutes()
    later = MARKET.replace(minute=interval - 1)
    assert sch.due_reason(later, sch._last_periodic, None) is None
    assert sch.due_reason(MARKET.replace(minute=interval), sch._last_periodic, None) == "periodic"


@pytest.mark.parametrize(
    "result", [[{"status": "ok"}, {"status": "no_auth"}], [{"status": "broker_error"}], "raise"]
)
def test_failed_event_sync_does_not_reset_the_periodic_timer(result):
    def fn():
        if result == "raise":
            raise RuntimeError("x")
        return result

    sch._sync_fn = fn
    before = datetime(2026, 11, 17, 10, 50)
    sch._last_periodic = before
    assert sch.trigger_sync(sch.EVENT_REASON) and wait_for(lambda: not sch._inflight)
    assert sch._last_periodic == before


def test_periodic_and_manual_syncs_keep_their_timer_rules():
    sch._sync_fn = lambda: [{"status": "ok"}]
    before = datetime(2026, 11, 17, 10, 50)
    sch._last_periodic = before
    for reason in ("manual", "boot", "eod"):
        assert sch.trigger_sync(reason) and wait_for(lambda: not sch._inflight)
    assert sch._last_periodic == before


def test_event_sync_runs_the_same_sync_function_as_periodic():
    seen = []
    sch._sync_fn = lambda: seen.append(threading.current_thread().name)
    for reason in ("periodic", sch.EVENT_REASON):
        assert sch.trigger_sync(reason) and wait_for(lambda: not sch._inflight)
    assert len(seen) == 2 and all(n.startswith("discretionary-ledger") for n in seen)


def test_on_done_callback_failure_is_contained():
    sch._sync_fn = lambda: None

    def bad(ok):
        raise RuntimeError("x")

    assert sch.trigger_sync(sch.EVENT_REASON, on_done=bad)
    assert wait_for(lambda: not sch._inflight)
    assert sch.trigger_sync("manual")  # the worker survived


# ---------------------------------------------------------------- latency (software only)


@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    from database import discretionary_book_db as book
    from database import discretionary_ledger_db as ledger
    from database import strategy_book_db as sb
    from services import discretionary_pnl_service as pnl

    eng = create_engine(
        f"sqlite:///{tmp_path / 'db.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    mods = (ledger, book, sb)
    originals = [m.engine for m in mods]
    for m in mods:
        m.db_session.remove()
        m.db_session.configure(bind=eng)
        m.Base.metadata.create_all(eng)
        monkeypatch.setattr(m, "engine", eng)
    se = tmp_path / "trades.db"
    con = sqlite3.connect(se)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, order_id TEXT, strategy TEXT)")
    con.commit()
    con.close()
    monkeypatch.setenv("SIGNAL_ENGINE_TRADES_DB", str(se))

    def calendar_down(day, exchange):
        raise RuntimeError("calendar unavailable in tests")

    monkeypatch.setattr(pnl, "_now", lambda: MARKET)
    monkeypatch.setattr(pnl, "_calendar_window", calendar_down)
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: False)
    monkeypatch.setattr(pnl, "_calendar_cache", {})
    monkeypatch.setattr(pnl, "_tables_ready", False)
    monkeypatch.delenv("DISCRETIONARY_RELIABLE_SINCE", raising=False)
    yield SimpleNamespace(engine=eng, pnl=pnl, ledger=ledger)
    for m, o in zip(mods, originals, strict=True):
        m.db_session.remove()
        m.db_session.configure(bind=o)


def _tradebook_row(flid, side, qty, price, tm):
    # recorded Flattrade TradeBook shape (masked), as in test_discretionary_ledger_service.fl_row
    return {
        "flid": flid,
        "fltm": f"17-11-2026 {tm}",
        "flqty": str(qty),
        "flprc": str(price),
        "norenordno": f"ORD-{flid}",
        "actid": "ACT",
        "exch": "NSE",
        "tsym": "TCS-EQ",
        "prd": "I",
        "trantype": side,
        "qty": "25",
        "avgprc": str(price),
        "norentm": "10:50:00 17-11-2026",
    }


def test_latency_event_to_summary_software_only(ledger_env, monkeypatch, capsys):
    """Event -> debounce timer -> real sync_once (mocked TradeBook, simulated read time) ->
    ledger row -> get_performance shows the fill. Prints the measured software latency."""
    from services import discretionary_ledger_service as svc

    debounce, read_s = 1.0, 0.116  # read_s: p50 of production ledger_sync_runs durations
    monkeypatch.setattr(sub, "_debounce_seconds", lambda: debounce)
    raw = [_tradebook_row("F1", "S", 25, 3500.5, "10:58:00")]
    auth = SimpleNamespace(name="alice", user_id="id-alice", broker="flattrade", is_revoked=False)
    marks = {}

    def tradebook(auth_token):
        marks["sync_start"] = time.perf_counter()
        time.sleep(read_s)
        return copy.deepcopy(raw)

    patches = [
        patch("database.auth_db.get_auth_token_dbquery", return_value=auth),
        patch("database.auth_db.get_auth_token", return_value="TOKEN-MASKED"),
        patch("utils.session.has_login_this_trading_session", return_value=True),
        patch("broker.flattrade.api.order_api.get_trade_book", side_effect=tradebook),
        patch(
            "broker.flattrade.mapping.order_data.get_oa_symbol",
            side_effect=lambda sym, exch: {"TCS-EQ": "TCS"}.get(sym),
        ),
    ]
    for p in patches:
        p.start()
    try:

        def one_sync():
            r = svc.sync_once("alice", lambda: MARKET)
            marks["row_visible"] = time.perf_counter()
            return [r]

        sch._sync_fn = one_sync
        t0 = time.perf_counter()
        sub.on_order_update(ev(symbol="TCS", action="SELL", quantity=25, filled=25))
        marks["handler_done"] = time.perf_counter()
        assert wait_for(lambda: "row_visible" in marks, timeout=10)
        assert wait_for(idle)
        perf = ledger_env.pnl.get_performance(period="all")
        marks["summary"] = time.perf_counter()
    finally:
        for p in patches:
            p.stop()
    assert perf["provisional"]["fills"] == 1  # unresolved (inside the grace), visible
    with ledger_env.engine.connect() as c:
        status = c.execute(text("SELECT status FROM ledger_sync_runs")).scalars().all()
    assert status == ["ok"]
    ms = {
        "handler_ms": (marks["handler_done"] - t0) * 1000,
        "event_to_sync_start_ms": (marks["sync_start"] - t0) * 1000,
        "timer_overhead_ms": (marks["sync_start"] - t0 - debounce) * 1000,
        "sync_total_ms": (marks["row_visible"] - marks["sync_start"]) * 1000,
        "sync_software_ms": (marks["row_visible"] - marks["sync_start"] - read_s) * 1000,
        "summary_ms": (marks["summary"] - marks["row_visible"]) * 1000,
        "event_to_summary_ms": (marks["summary"] - t0) * 1000,
    }
    with capsys.disabled():
        print("\nLATENCY " + " ".join(f"{k}={v:.1f}" for k, v in ms.items()))
    assert ms["timer_overhead_ms"] < 500
