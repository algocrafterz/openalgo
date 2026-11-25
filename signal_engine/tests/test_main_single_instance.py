"""The engine runs as `python -m signal_engine.main`, so main.py is loaded as `__main__`.

tracker.py reaches back into main through lazy `from signal_engine.main import ...`. With no
alias for `signal_engine.main`, each such import loaded a SECOND copy of main.py with its own
empty `tracker = PositionTracker(...)`, its own exit locks, its own risk engine and its own
RiskStore. The unresolved-order sweep then looked up positions on that empty tracker, found
none, and never restored a stop or recorded a late exit fill (2026-11-25: six exits
auto-cancelled for no quote, positions left without a stop for 90 to 100 minutes; ten late
fills booked under the cancelled stop's order id).

The first group of tests runs main.py as `__main__` in a subprocess (the same
`runpy._run_module_as_main` path `python -m` uses, with RuntimeWarning turned into an error) and
probes through the same imports tracker.py performs. Every network and database edge is
replaced BEFORE the engine module executes, so a stray second copy would bind the same stubs and
nothing leaves the process: only the object the code reaches can differ. The last group runs in
process and pins the race the sweep's stop restore had with a new signal exit.
"""

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import main, order_reconcile
from signal_engine.models import Action, Order
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.timeutils import IST
from signal_engine.tracker import PositionTracker

_REPO = Path(__file__).resolve().parents[2]

_DRIVER = textwrap.dedent(
    """
    import asyncio, json, os, runpy, sys
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    scenario = sys.argv[1]
    tmp = sys.argv[2]  # the test's tmp_path: pytest removes it
    os.environ["LOG_DIR"] = os.path.join(tmp, "log")
    import signal_engine.risk_store as risk_store
    risk_store.RISK_DB_PATH = os.path.join(tmp, "risk.db")
    import signal_engine.logger_setup as logger_setup
    logger_setup.setup_logger = lambda: None
    import signal_engine.startup as startup
    import dataclasses
    import signal_engine.config as config
    # The adopted entry must be given its stop whatever the operator's config says.
    config.settings = dataclasses.replace(config.settings, bracket_enabled=True)
    import signal_engine.api_client as api_client
    import signal_engine.db as db
    import signal_engine.tracker as tracker_module
    from signal_engine import order_reconcile as oc
    from signal_engine.models import (
        Action, Direction, Order, OrderStatus, Signal, TradeResult)
    from signal_engine.timeutils import IST

    STRAT, SYM = "EVWAP-EARLY-PY", "SUNPHARMA"
    stamp = (datetime.now(IST) + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")

    def row(oid, action, status="complete"):
        return {"orderid": oid, "symbol": SYM, "action": action, "quantity": 50,
                "order_status": status, "timestamp": stamp, "strategy": STRAT}

    book = []
    if scenario == "sweep_filled":
        book = [row("EXIT-FILLED-9", "SELL")]
    if scenario == "adopt":
        book = [row("ENTRY-LATE-7", "BUY")]

    sent = []

    async def fetch_orderbook(*a, **k):
        return list(book)

    async def fetch_order_status(*a, **k):
        return "cancelled"  # the stop the engine cancelled before sending the exit

    async def fetch_own_position(self, *a, **k):
        return 50  # the broker still holds exactly the tracked position

    async def send_order(*a, **k):
        sent.append(a)
        return TradeResult(order_id="SENT-1", status=OrderStatus.SUCCESS, message="ok")

    async def send_bracket_legs(*a, **k):
        sent.append(("bracket",))
        return TradeResult(order_id="NEW-STOP-1", status=OrderStatus.SUCCESS, message="ok"), None

    async def fetch_order_fill_price(*a, **k):
        return 1800.0

    api_client.fetch_orderbook = fetch_orderbook
    api_client.fetch_order_status = fetch_order_status
    import signal_engine.executor as executor
    executor.send_bracket_legs = send_bracket_legs
    executor.send_order = send_order
    executor.cancel_live_protective_orders = AsyncMock(return_value=None)
    executor.place_sl_order = AsyncMock(return_value=TradeResult(
        order_id="NEW-STOP-1", status=OrderStatus.SUCCESS, message="ok"))
    api_client.fetch_order_fill_price = fetch_order_fill_price
    db.fetch_order_ids_today = lambda symbol: set()
    db.save = lambda *a, **k: 1
    db.update_entry_fill = lambda *a, **k: None
    if scenario == "shared_net":
        async def fetch_net_position(*a, **k):
            return 80  # one broker row for both strategies: 50 + 30, no strategy dimension

        tracker_module.fetch_net_position = fetch_net_position
        tracker_module.fetch_positionbook = AsyncMock(return_value=None)
    else:
        tracker_module.PositionTracker.fetch_own_position = fetch_own_position
        tracker_module.PositionTracker.fetch_own_position_proven = fetch_own_position
    tracker_module.PositionTracker._own_close_qty = AsyncMock(return_value=50)
    tracker_module.PositionTracker._can_begin = lambda *a, **k: True

    def leg():
        pos = tracker_module.TrackedPosition(
            symbol=SYM, strategy=STRAT, exchange="NSE", product="MIS",
            entry_price=1800.0, quantity=50, sl=1780.0, tp=0.0, direction=Direction.LONG,
            sl_order_id="STOP-CANCELLED-1", exit_pending=True)
        pos.claimed_exit = "TIME_EXIT"
        return pos

    def uncertain_exit():
        oc.mark_uncertain(oc.UncertainOrder(
            STRAT, SYM, "SELL", 50, datetime.now(IST).replace(microsecond=0), "EXIT"))

    def probe(argv):
        running = sys.modules["__main__"]
        # The exact imports tracker.py performs lazily.
        from signal_engine.main import (
            _exit_locks, _get_exit_lock, _release_exit_lock, sweep_unresolved_orders)
        import signal_engine.main as imported
        out = {}
        if scenario == "identity":
            pos = leg()
            running.tracker.register(pos)
            lock = running._get_exit_lock(SYM, STRAT)
            out = {
                "alias_is_running_module": imported is running,
                "module_objects": len({id(m) for m in sys.modules.values()
                                       if getattr(m, "__file__", "") and m.__file__.endswith("signal_engine/main.py")}),
                "sweep_globals_are_running": sweep_unresolved_orders.__globals__ is running.__dict__,
                "lock_globals_are_running": _get_exit_lock.__globals__ is running.__dict__,
                "release_globals_are_running": _release_exit_lock.__globals__ is running.__dict__,
                "locks_dict_shared": _exit_locks is running._exit_locks,
                "lock_registered": bool(running._exit_locks),
            }
            running.tracker.unregister(SYM, STRAT)
            out["unregister_empties_running_locks"] = not running._exit_locks
            out["lock_object_is_same"] = lock is not None
        elif scenario in ("sweep_absent", "sweep_filled"):
            pos = leg()
            running.tracker.register(pos)
            uncertain_exit()
            asyncio.run(sweep_unresolved_orders())
            out = {
                "sl_order_id": pos.sl_order_id,
                "claimed_exit": pos.claimed_exit,
                "exit_pending": pos.exit_pending,
                "hold_cleared": oc.get_uncertain(STRAT, SYM) is None,
            }
        elif scenario == "shared_net":
            pos = leg()
            running.tracker.register(pos)
            other = leg()
            other.strategy, other.quantity, other.sl_order_id = "BREAKOUT-PY", 30, "OTHER-STOP"
            other.exit_pending = False
            running.tracker.register(other)
            uncertain_exit()
            own = asyncio.run(running.tracker.fetch_own_position(SYM, STRAT, "NSE", "MIS"))
            asyncio.run(sweep_unresolved_orders())
            out = {"own": own, "sl_order_id": pos.sl_order_id, "exit_pending": pos.exit_pending,
                   "other_stop": other.sl_order_id}
        elif scenario == "lock_sharing":
            pos = leg()
            pos.exit_pending = False
            running.tracker.register(pos)
            tracker_module._EXIT_LOCK_WAIT_SECONDS = 0.2

            async def run():
                lock = running._get_exit_lock(SYM, STRAT)  # a signal exit is mid-flight
                await lock.acquire()
                await running.tracker._close_strategy(STRAT, {SYM})
                lock.release()

            asyncio.run(run())
            out = {"orders_sent": len(sent),
                   "still_tracked": running.tracker.find_position(SYM, STRAT) is pos}
        elif scenario == "release_slot":
            sig = SimpleNamespace(strategy=STRAT, symbol=SYM)
            running._hold_watched_entry_slot(sig)
            before = running.risk_engine.pending_entries_for(STRAT)
            oc.mark_uncertain(oc.UncertainOrder(
                STRAT, SYM, "BUY", 50, datetime.now(IST).replace(microsecond=0), "ENTRY"))
            asyncio.run(sweep_unresolved_orders())
            out = {"before": before,
                   "after": running.risk_engine.pending_entries_for(STRAT),
                   "hold_cleared": oc.get_uncertain(STRAT, SYM) is None}
        elif scenario == "adopt":
            sig = Signal(strategy=STRAT, direction=Direction.LONG, symbol=SYM, entry=1800.0,
                         sl=1780.0, tp=0.0, sig_id="adopt-1")
            order = Order(symbol=SYM, exchange="NSE", action=Action.BUY, quantity=50, price=0.0,
                          order_type="MARKET", product="MIS", strategy_tag=STRAT)
            running._hold_watched_entry_slot(sig)
            oc.mark_uncertain(oc.UncertainOrder(
                STRAT, SYM, "BUY", 50, datetime.now(IST).replace(microsecond=0), "ENTRY",
                signal=sig, order=order))
            asyncio.run(sweep_unresolved_orders())
            pos = running.tracker.find_position(SYM, STRAT)
            out = {"tracked": pos is not None,
                   "stop_id": pos.sl_order_id if pos else None,
                   "tracked_count": running.tracker.tracked_count,
                   "open_positions": running.risk_engine._state(STRAT).open_positions,
                   "reserved_after": running.risk_engine.pending_entries_for(STRAT),
                   "bracket_orders": sum(1 for s in sent if s == ("bracket",))}
        print(json.dumps(out))
        return True

    startup.run_health_check_cli = probe
    runpy._run_module_as_main("signal_engine.main")
    """
)


def _as_main(scenario: str, tmp_path) -> dict:
    env = dict(os.environ, PYTHONPATH=str(_REPO))
    proc = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-c", _DRIVER, scenario, str(tmp_path)],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_every_lazy_import_from_tracker_reaches_the_running_engine_module(tmp_path):
    seen = _as_main("identity", tmp_path)
    assert seen["alias_is_running_module"] is True
    assert seen["module_objects"] == 1
    assert seen["sweep_globals_are_running"] is True
    assert seen["lock_globals_are_running"] is True
    assert seen["release_globals_are_running"] is True


def test_the_exit_lock_table_is_the_one_the_tracker_cleans_up(tmp_path):
    seen = _as_main("identity", tmp_path)
    assert seen["locks_dict_shared"] is True
    assert seen["lock_registered"] is True
    # tracker.unregister drops the lock through its own lazy import: on a second copy it emptied
    # that copy's table and left the running engine's lock behind.
    assert seen["unregister_empties_running_locks"] is True


def test_absent_exit_on_a_tracked_position_gets_its_stop_back_and_its_claim_cleared(tmp_path):
    seen = _as_main("sweep_absent", tmp_path)
    assert seen["hold_cleared"] is True
    # The cancelled stop id is dropped so the poll's missing-stop retry places a new stop.
    assert seen["sl_order_id"] == ""
    assert seen["claimed_exit"] == ""
    assert seen["exit_pending"] is False


def test_late_filled_exit_records_the_real_exit_order_id_on_the_tracked_position(tmp_path):
    seen = _as_main("sweep_filled", tmp_path)
    assert seen["hold_cleared"] is True
    # book_close() writes this id as the closing order: it must be the exit, not the cancelled stop.
    assert seen["sl_order_id"] == "EXIT-FILLED-9"


def test_a_time_exit_waits_on_the_running_engines_signal_exit_lock_and_sends_no_order(tmp_path):
    """E-06 under the real launch: the lock a signal exit holds is the lock the time exit sees."""
    seen = _as_main("lock_sharing", tmp_path)
    assert seen["orders_sent"] == 0
    assert seen["still_tracked"] is True


def test_a_net_position_shared_by_two_strategies_is_split_by_the_running_trackers_books(tmp_path):
    seen = _as_main("shared_net", tmp_path)
    assert seen["own"] == 50  # 80 at the broker less the other strategy's 30
    # The broker holds exactly the tracked 50 for this strategy, so its stop is restored; the
    # other strategy's stop is not touched.
    assert seen["sl_order_id"] == ""
    assert seen["exit_pending"] is False
    assert seen["other_stop"] == "OTHER-STOP"


def test_a_released_entry_watch_frees_the_slot_on_the_running_risk_engine(tmp_path):
    seen = _as_main("release_slot", tmp_path)
    assert seen["before"] == 1
    assert seen["after"] == 0
    assert seen["hold_cleared"] is True


def test_an_adopted_late_entry_lands_on_the_running_tracker_and_risk_engine_once(tmp_path):
    seen = _as_main("adopt", tmp_path)
    assert seen["tracked"] is True
    assert seen["stop_id"] == "NEW-STOP-1"
    assert seen["tracked_count"] == 1
    assert seen["open_positions"] == 1  # recorded once, on the engine the entry gates read
    assert seen["reserved_after"] == 0  # the watch reservation became the recorded trade
    assert seen["bracket_orders"] == 1  # one stop, never two


# --- the sweep's stop restore against a new signal exit on the same leg ---------------------------

_SYM, _STRAT = "SUNPHARMA", "EVWAP-EARLY-PY"


def _hold_since(seconds=90):
    """When a test hold was placed: `seconds` ago, but never before today's midnight.

    The sweep drops a hold dated before today, so a fixed 90 s offset would fail the tests in the
    first 90 s after midnight IST.
    """
    now = datetime.now(IST)
    return max(
        now - timedelta(seconds=seconds), now.replace(hour=0, minute=0, second=0, microsecond=0)
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for table in (
        order_reconcile._uncertain,
        order_reconcile._last_checked,
        order_reconcile._last_alerted,
    ):
        table.clear()
    monkeypatch.setattr(main, "_exit_locks", {})
    yield
    for table in (
        order_reconcile._uncertain,
        order_reconcile._last_checked,
        order_reconcile._last_alerted,
    ):
        table.clear()


def _held_exit_on_a_tracked_leg():
    tracker = PositionTracker(_make_engine())
    pos = _make_position(
        symbol=_SYM, strategy=_STRAT, quantity=50, sl_order_id="STOP-CANCELLED-1", exit_pending=True
    )
    pos.claimed_exit = "TIME_EXIT"
    tracker.register(pos)
    order_reconcile.mark_uncertain(
        order_reconcile.UncertainOrder(_STRAT, _SYM, "SELL", 50, _hold_since(), "EXIT")
    )
    return tracker, pos


def _sweep_patches(tracker, status):
    return (
        patch.object(main, "tracker", tracker),
        patch.object(main, "fetch_orderbook", AsyncMock(return_value=[])),
        patch.object(main, "fetch_order_status", status),
        patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
        patch.object(PositionTracker, "fetch_own_position", AsyncMock(return_value=50)),
    )


@pytest.mark.asyncio
async def test_a_signal_exit_that_starts_during_the_stop_status_read_keeps_ownership():
    tracker, pos = _held_exit_on_a_tracked_leg()
    in_status_read, finish_read = asyncio.Event(), asyncio.Event()

    async def status(*_a, **_k):
        in_status_read.set()
        await finish_read.wait()
        return "cancelled"

    seen = {}

    async def signal_exit():
        # What _handle_exit does: take the leg's lock, then claim the position.
        async with main._get_exit_lock(_SYM, _STRAT):
            pos.exit_pending = True
            await asyncio.sleep(0.02)  # its own cancel and send awaits
            seen["exit_pending_mid_exit"] = pos.exit_pending

    patches = _sweep_patches(tracker, status)
    for p in patches:
        p.start()
    try:
        sweep = asyncio.create_task(main.sweep_unresolved_orders())
        await asyncio.wait_for(in_status_read.wait(), 5)
        exit_task = asyncio.create_task(signal_exit())
        await asyncio.sleep(0.01)  # the signal exit is now at the lock (or inside it, on old code)
        finish_read.set()
        await asyncio.wait_for(asyncio.gather(sweep, exit_task), 5)
    finally:
        for p in patches:
            p.stop()
    # The restore must not clear exit_pending under a running exit: with it cleared the poll's
    # missing-stop retry places a stop beside the exit's own order.
    assert seen["exit_pending_mid_exit"] is True
    assert pos.exit_pending is True


@pytest.mark.asyncio
async def test_a_leg_whose_exit_lock_is_held_is_not_restored_and_reads_no_stop_status():
    tracker, pos = _held_exit_on_a_tracked_leg()
    status = AsyncMock(return_value="cancelled")
    lock = main._get_exit_lock(_SYM, _STRAT)
    await lock.acquire()  # a signal exit is running
    patches = _sweep_patches(tracker, status)
    for p in patches:
        p.start()
    try:
        await main._restore_stop_after_absent_exit(order_reconcile.get_uncertain(_STRAT, _SYM))
    finally:
        for p in patches:
            p.stop()
        lock.release()
    status.assert_not_awaited()
    assert pos.sl_order_id == "STOP-CANCELLED-1"
    assert pos.exit_pending is True


@pytest.mark.asyncio
async def test_the_restore_takes_the_lock_only_for_the_read_and_gives_it_back():
    tracker, pos = _held_exit_on_a_tracked_leg()
    patches = _sweep_patches(tracker, AsyncMock(return_value="cancelled"))
    for p in patches:
        p.start()
    try:
        await main.sweep_unresolved_orders()
    finally:
        for p in patches:
            p.stop()
    assert pos.sl_order_id == "" and pos.exit_pending is False and pos.claimed_exit == ""
    assert not main._get_exit_lock(_SYM, _STRAT).locked()


# --- two paths settling the same unconfirmed order ----------------------------------------------
#
# The sweep and a signal path (a new exit, or a new entry) both reconcile the same hold. Each reads
# the order book first, so one can finish while the other is still reading. Whoever finds the hold
# gone after its read must not act: the corrective order, the adoption and the stop belong to the
# path that cleared it.


def _book_row(oid, action, qty=50, status="complete"):
    return {
        "orderid": oid,
        "symbol": _SYM,
        "action": action,
        "quantity": qty,
        "order_status": status,
        "timestamp": (datetime.now(IST) + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": _STRAT,
    }


class _GatedBook:
    """An order book whose first read by the task named `slow` waits for `release`."""

    def __init__(self, rows, slow):
        self.rows, self.slow = rows, slow
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self._held_once = False

    async def __call__(self, *_a, **_k):
        if asyncio.current_task().get_name() == self.slow and not self._held_once:
            self._held_once = True
            self.entered.set()
            await self.release.wait()
        return list(self.rows)


def _duplicate_exit_setup():
    """A 50-share long whose exit filled twice: broker net is -50 (flat plus one extra sold)."""
    tracker = PositionTracker(_make_engine())
    pos = _make_position(symbol=_SYM, strategy=_STRAT, quantity=50, sl_order_id="STOP-CANCELLED-1")
    pos.exit_pending = True
    tracker.register(pos)
    pending = order_reconcile.UncertainOrder(_STRAT, _SYM, "SELL", 50, _hold_since(), "EXIT")
    order_reconcile.mark_uncertain(pending)
    rows = [_book_row("EXIT-A", "SELL"), _book_row("EXIT-B", "SELL")]
    return tracker, pos, rows


async def _settle_through_the_signal_path(pos):
    exit_order = main.build_exit_order(
        symbol=_SYM,
        exchange=pos.exchange,
        quantity=50,
        product=pos.product,
        strategy_tag=_STRAT,
        direction=main.Direction.SHORT,
    )
    return await main._settle_prior_uncertain_exit(exit_order, pos)


async def _race_exit_paths(slow):
    tracker, pos, rows = _duplicate_exit_setup()
    book = _GatedBook(rows, slow)
    corrective = AsyncMock(
        return_value=main.TradeResult(order_id="FIX-1", status=main.OrderStatus.SUCCESS)
    )
    patches = (
        patch.object(main, "tracker", tracker),
        patch.object(main, "fetch_orderbook", book),
        patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
        patch.object(main, "send_order", corrective),
        patch.object(PositionTracker, "fetch_own_position", AsyncMock(return_value=-50)),
    )
    for p in patches:
        p.start()
    try:
        first_name, second_name = ("sweep", "signal") if slow == "signal" else ("signal", "sweep")
        jobs = {
            "sweep": lambda: main.sweep_unresolved_orders(),
            "signal": lambda: _settle_through_the_signal_path(pos),
        }
        slow_task = asyncio.create_task(jobs[slow](), name=slow)
        await asyncio.wait_for(book.entered.wait(), 5)
        fast = "sweep" if slow == "signal" else "signal"
        await asyncio.wait_for(asyncio.create_task(jobs[fast](), name=fast), 5)
        book.release.set()
        await asyncio.wait_for(slow_task, 5)
    finally:
        for p in patches:
            p.stop()
    return corrective


@pytest.mark.asyncio
async def test_a_duplicate_exit_fill_is_corrected_once_when_a_signal_exit_settles_the_hold_first():
    corrective = await _race_exit_paths(slow="sweep")
    assert corrective.await_count == 1
    assert order_reconcile.get_uncertain(_STRAT, _SYM) is None


@pytest.mark.asyncio
async def test_a_duplicate_exit_fill_is_corrected_once_when_the_sweep_settles_the_hold_first():
    corrective = await _race_exit_paths(slow="signal")
    assert corrective.await_count == 1
    assert order_reconcile.get_uncertain(_STRAT, _SYM) is None


async def _race_entry_paths(slow):
    tracker = PositionTracker(_make_engine())
    engine = tracker._risk_engine
    signal = main.Signal(
        strategy=_STRAT,
        direction=main.Direction.LONG,
        symbol=_SYM,
        entry=1800.0,
        sl=1780.0,
        tp=0.0,
        sig_id="adopt-race-1",
    )
    order = Order(
        symbol=_SYM,
        exchange="NSE",
        action=Action.BUY,
        quantity=50,
        price=0.0,
        order_type="MARKET",
        product="MIS",
        strategy_tag=_STRAT,
    )
    pending = order_reconcile.UncertainOrder(
        _STRAT, _SYM, "BUY", 50, _hold_since(), "ENTRY",
        signal=signal, order=order,
    )  # fmt: skip
    order_reconcile.mark_uncertain(pending)
    book = _GatedBook([_book_row("ENTRY-LATE-7", "BUY")], slow)
    bracket = AsyncMock(
        return_value=(
            main.TradeResult(order_id="NEW-STOP-1", status=main.OrderStatus.SUCCESS),
            None,
        )
    )
    settings = dataclasses.replace(main.settings, bracket_enabled=True)
    patches = (
        patch.object(main, "tracker", tracker),
        patch.object(main, "risk_engine", engine),
        patch.object(main, "settings", settings),
        patch.object(main, "fetch_orderbook", book),
        patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
        patch.object(main, "send_bracket_legs", bracket),
        patch.object(main, "fetch_order_fill_price", AsyncMock(return_value=1800.0)),
        patch.object(main, "save", MagicMock(return_value=1)),
        patch.object(main, "update_entry_fill", MagicMock()),
    )
    for p in patches:
        p.start()
    try:
        jobs = {
            "sweep": lambda: main.sweep_unresolved_orders(),
            "signal": lambda: main._entry_held_by_unresolved_order(signal),
        }
        slow_task = asyncio.create_task(jobs[slow](), name=slow)
        await asyncio.wait_for(book.entered.wait(), 5)
        fast = "sweep" if slow == "signal" else "signal"
        await asyncio.wait_for(asyncio.create_task(jobs[fast](), name=fast), 5)
        book.release.set()
        await asyncio.wait_for(slow_task, 5)
    finally:
        for p in patches:
            p.stop()
    return tracker, engine, bracket


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", ["sweep", "signal"])
async def test_a_late_entry_is_adopted_once_whichever_path_reads_the_book_last(slow):
    tracker, engine, bracket = await _race_entry_paths(slow)
    assert tracker.tracked_count == 1
    assert bracket.await_count == 1  # one stop
    assert engine._state(_STRAT).open_positions == 1
    assert order_reconcile.get_uncertain(_STRAT, _SYM) is None
