"""Shared test fixtures and factories for signal engine tests."""

import os

# utils.logging attaches a JSON handler on $LOG_DIR/errors.jsonl at import time, so every
# error these tests deliberately provoke (mock RuntimeErrors, simulated outages) was landing
# in the operator's production log, and setup_logging's startup truncation to 1000 lines could
# evict real errors. Assigned unconditionally and BEFORE any project import below, the same
# guarantee test/conftest.py gives the OpenAlgo core suite.
os.environ["LOG_DIR"] = "log/test"

# signal_engine.main builds RiskStore(RISK_DB_PATH) at import and its engine defaults to mode
# "live", so every test that imported main opened the operational signal_engine/data/risk.db
# and any unpatched record_trade()/record_close() wrote a PORTFOLIO/live row for today into it
# (rows dated on weekends, 2026-10-24 and 2026-10-25, are in that file). Repointed here, BEFORE
# any project import can bind the path, so the real file is unreachable from the suite.
import tempfile  # noqa: E402

import signal_engine.risk_store as _risk_store_module  # noqa: E402

_risk_store_module.RISK_DB_PATH = os.path.join(
    tempfile.mkdtemp(prefix="signal_engine_tests_"), "risk.db"
)

# logger_setup builds its file sinks and the errors_*.jsonl sink (plus its 90-day pruning of
# errors_*.jsonl) from the cwd-relative directory "signal_engine/logs". A test that reaches
# setup_logger() (main.main(), the startup runner) from the repo root therefore wrote
# "Signal Engine starting" and test errors into the operator's real log files, and the sinks it
# left behind kept doing so for every later test. The directory is read through _log_dir() and
# _error_log_dir() at call time; they are replaced here, BEFORE any test runs, with versions that
# send the real signal_engine/logs (however the relative path resolves) to a throwaway directory.
# A relative path that resolves elsewhere (a test that chdirs into tmp_path) is left alone, so
# tests that assert on the files they create keep working.
import signal_engine.logger_setup as _logger_setup_module  # noqa: E402

_REAL_LOG_DIR = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(_logger_setup_module.__file__)), "logs")
)
_TEST_LOG_DIR = os.path.join(tempfile.mkdtemp(prefix="signal_engine_test_logs_"), "logs")


def _redirect_real_log_dir(configured: str) -> str:
    if os.path.realpath(configured) == _REAL_LOG_DIR:
        return _TEST_LOG_DIR
    return configured


_logger_setup_module._log_dir = lambda: _redirect_real_log_dir(_logger_setup_module.LOG_DIR)
_logger_setup_module._error_log_dir = lambda: _redirect_real_log_dir(
    _logger_setup_module._ERROR_LOG_DIR
)

import httpx  # noqa: E402
import pytest  # noqa: E402

from signal_engine.models import Direction, Signal  # noqa: E402
from signal_engine.strategies import ORB  # noqa: E402


def make_signal(**overrides) -> Signal:
    """Create a Signal with sensible defaults. Override any field via kwargs."""
    defaults = {
        "strategy": ORB,
        "direction": Direction.LONG,
        "symbol": "RELIANCE",
        "entry": 2500.0,
        "sl": 2485.0,
        "tp": 2540.0,
        "raw_message": "test",
    }
    defaults.update(overrides)
    return Signal(**defaults)


@pytest.fixture(autouse=True)
def _never_touch_the_real_trades_db(tmp_path, monkeypatch):
    """Point every test's persistence at a throwaway file.

    Not paranoia — it already happened. `save_declined()` was added to main's decline paths
    while the existing entry-pipeline tests patched only `save`, so an ordinary `pytest` run
    wrote six rows into signal_engine/data/trades.db, the live audit trail the ledger reads.
    Patching each new writer at each call site is a rule nobody can be relied on to follow;
    making the real path unreachable from a test is a guarantee.

    Autouse and session-wide, so a future writer is covered before anyone remembers it exists.

    Same guarantee for the day-summary "already sent today" marker: individual tests that
    exercise send_day_summary() have so far remembered to patch
    signal_engine.tracker._DAY_SUMMARY_MARKER (or _mark_summary_sent) themselves, but nothing
    made that structural. 2026-09-17: the real marker file's mtime shows it was written
    mid-morning with only a handful of the day's trades reflected - most plausibly some
    exercise of the real send path that skipped that per-test patch - which then silently
    suppressed the genuine 14:45 EOD summary for the rest of the day. Redirecting it here,
    the same way trades.db is redirected, makes that class of leak impossible regardless of
    whether any individual test remembers to isolate it.
    """
    monkeypatch.setattr("signal_engine.db._DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setattr("signal_engine.tracker._DAY_SUMMARY_MARKER", str(tmp_path / "day_summary"))


async def _blocked_post(self, url, *_args, **_kwargs):
    """Replacement for httpx.AsyncClient.post — see _block_real_openalgo_api_calls.

    Blocks the outbound REQUEST, not construction: test_resource_lifecycle.py legitimately
    builds real httpx.AsyncClient instances to test the shared-client singleton's identity
    and close() behaviour, and never issues a request while doing it.
    """
    raise RuntimeError(
        f"A test tried to POST {url!r} via a real httpx.AsyncClient, which would hit the "
        "live OpenAlgo API. Mock send_order/place_sl_order (or the specific api_client "
        "fetch_*/cancel_* function) at its call site's module — e.g. "
        "signal_engine.main.send_order or signal_engine.tracker.place_sl_order, not just "
        "signal_engine.executor.send_order, since main.py/tracker.py each hold their own "
        "'from signal_engine.executor import send_order'-style binding that a patch on the "
        "executor module does not reach. A test that needs to exercise the real HTTP layer "
        "(e.g. test_api_client.py) patches httpx.AsyncClient itself, which temporarily "
        "overrides this fixture for its own scope."
    )


@pytest.fixture(autouse=True)
def _entry_clock_is_mid_morning(monkeypatch):
    """main._now_ist feeds the entry gate that declines MIS entries after the time exit. A test
    must not depend on the hour it runs at: the clock reads 10:30 IST unless the test sets it."""
    from datetime import datetime

    from signal_engine.timeutils import IST

    monkeypatch.setattr(
        "signal_engine.main._now_ist",
        lambda: datetime(2026, 11, 12, 10, 30, 0, tzinfo=IST),
    )


@pytest.fixture(autouse=True)
def _fresh_shared_api_client(monkeypatch):
    """api_client._client is a lazily cached httpx.AsyncClient. A test that reaches the real
    _get_client() (even through the blocked-POST guard) leaves it cached, and a later test that
    patches httpx.AsyncClient would get that stale client instead of its mock. Each test starts
    with no cached client; monkeypatch restores the previous value afterwards. The value is set
    to None, not closed: an unused client holds no connection, and close_client() is async."""
    monkeypatch.setattr("signal_engine.api_client._client", None)


def _wipe_order_reconcile():
    """order_reconcile's uncertain-order registry is process-wide: an entry-timeout test that ends
    with a late-fill watch registered would otherwise hold the next test's entry for that symbol."""
    from signal_engine import order_reconcile

    order_reconcile._uncertain.clear()
    order_reconcile._last_checked.clear()
    order_reconcile._last_alerted.clear()


@pytest.fixture(autouse=True)
def _fresh_unconfirmed_stop_registry(monkeypatch):
    """executor._UNCONFIRMED_SL is process-wide state: a test that ends on an unresolved SL
    timeout would otherwise make the next test for the same key reconcile first.

    Also gives the tracker an empty order book by default: its close paths now sweep for
    untracked stops, and the real read retries with sleeps (the HTTP call itself is blocked
    below). A test that cares about the book patches signal_engine.tracker.fetch_orderbook."""
    from unittest.mock import AsyncMock

    from signal_engine import executor

    monkeypatch.setattr("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=[]))
    monkeypatch.setattr("signal_engine.main.fetch_orderbook", AsyncMock(return_value=[]))
    executor._UNCONFIRMED_SL.clear()
    executor._PENDING_DUPLICATE_STOPS.clear()
    executor._LAST_UNKNOWN_SEND.clear()
    executor._EPISODE_START.clear()
    _wipe_order_reconcile()
    yield
    executor._UNCONFIRMED_SL.clear()
    executor._PENDING_DUPLICATE_STOPS.clear()
    executor._LAST_UNKNOWN_SEND.clear()
    executor._EPISODE_START.clear()
    _wipe_order_reconcile()


@pytest.fixture(autouse=True)
def _block_real_openalgo_api_calls(monkeypatch):
    """Make a real HTTP call to the live OpenAlgo API impossible from a test.

    The network-call counterpart to _never_touch_the_real_trades_db above, for the exact
    same reason: it already happened. 2026-09-24: an ordinary `pytest signal_engine/tests/`
    run placed 20+ real SELL SL-M orders against INFY and TCS on the LIVE OpenAlgo sandbox —
    quantity=50, trigger=2485.0, strategy=ORB, all values traced straight back to this
    file's own make_signal()/tracker_fixtures.py defaults. No test intended to place a real
    order; some send_order/place_sl_order mock either wasn't applied for that code path or
    targeted the wrong module-qualified name (see the RuntimeError message above for why that
    is easy to get wrong here). The trades.db incident above was fixed by making the real
    path structurally unreachable rather than trusting every test to mock every writer;
    this is that same guarantee for outbound HTTP.
    """
    monkeypatch.setattr(httpx.AsyncClient, "post", _blocked_post)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_preflight: run record_run's real dataset preflight (no stub)")


@pytest.fixture(autouse=True)
def _published_dataset_stub(request, monkeypatch):
    """record_run needs a published immutable dataset version (plan D20). Test roots have none,
    and most record_run tests inject an `execute` that never reads data, so they get a READY
    resolution that touches no store. Tests of the real preflight mark themselves
    `real_preflight`. record_run is imported here so the stub never depends on import order."""
    if request.node.get_closest_marker("real_preflight"):
        return
    from signal_engine.backtest import record_run as rr
    from signal_engine.tests.published_fixtures import fake_resolution

    monkeypatch.setattr(rr, "prepare_data", lambda *a, **k: fake_resolution())


@pytest.fixture(autouse=True)
def _published_reproduce_stub(request, monkeypatch):
    """reproduce rebuilds a schema 4 run from its recorded published version; test runs record the
    fake version from `_published_dataset_stub`, so they reproduce against the same fake."""
    if request.node.get_closest_marker("real_preflight"):
        return
    from signal_engine.backtest import reproduce as rp
    from signal_engine.tests.published_fixtures import fake_resolution

    monkeypatch.setattr(rp, "_resolution_for", lambda manifest: fake_resolution())


@pytest.fixture(autouse=True)
def _no_operator_entry_hold(tmp_path, monkeypatch):
    """An operator's real signal_engine/data/ENTRY_HOLD file (placed while the engine is held after a
    restart) must never change a test's outcome: every test sees no hold unless it sets one."""
    from signal_engine import mode_guard

    monkeypatch.setattr(mode_guard, "HOLD_FILE", tmp_path / "no-ENTRY_HOLD")


@pytest.fixture(autouse=True)
def _never_touch_operational_state_files(tmp_path, monkeypatch):
    """Redirect the remaining default-path operational files (see test_operational_db_isolation).

    risk.db is repointed at import above. The runner state database cannot be repointed through
    its module constant, because RunnerStateStore binds the path as a default argument when the
    class is built, so the default itself is replaced. The card state, day-summary pin state,
    bug tracker and shadow tick directory are read at call time and are patched by name.
    """
    from signal_engine import notifier, strategy_cards
    from signal_engine.bugs import store as bug_store
    from signal_engine.pipeline.state import RunnerStateStore
    from signal_engine.shadow import store as shadow_store

    defaults = RunnerStateStore.__init__.__defaults__
    monkeypatch.setattr(
        RunnerStateStore.__init__,
        "__defaults__",
        (str(tmp_path / "runner_state.db"),) + tuple(defaults[1:]),
    )
    monkeypatch.setattr(strategy_cards, "_STATE_PATH", str(tmp_path / "strategy_card_state.json"))
    monkeypatch.setattr(
        notifier, "_DAY_SUMMARY_PIN_STATE_PATH", str(tmp_path / "day_summary_pin_state.json")
    )
    monkeypatch.setattr(bug_store, "TRACKER_PATH", str(tmp_path / "bugs.json"))
    monkeypatch.setattr(shadow_store, "DATA_DIR", tmp_path / "shadow")


_TELEGRAM_HOST = "api.telegram.org"


def _is_telegram_url(url) -> bool:
    """True when `url` (str, httpx.URL, urllib Request) targets the Telegram Bot API host."""
    from urllib.parse import urlparse

    target = getattr(url, "full_url", url)
    host = getattr(target, "host", None) or urlparse(str(target)).hostname
    return (host or "").lower() == _TELEGRAM_HOST


def _telegram_refused(url) -> RuntimeError:
    return RuntimeError(
        f"A test tried to reach {_TELEGRAM_HOST} ({str(url)!r}), which would send a REAL Telegram "
        "message. Patch the sender at its call site (alerts.send, critical.notifier or the "
        "module's own alert function). 2026-11-11 to 2026-11-13: unmocked BreakingTrade critical "
        "alerts from test_breakingtrade_login_guard.py reached the live channel about 56 times."
    )


@pytest.fixture(autouse=True)
def _block_real_telegram_sends(monkeypatch):
    """Make a real Telegram message impossible from a test, on every outbound path.

    The synchronous counterpart to _block_real_openalgo_api_calls, which blocks only
    httpx.AsyncClient.post. alerts.send() uses the synchronous httpx.post, so a test that ran the
    real BreakingTrade poller loop sent real CRITICAL banners to the trader's channel. Only
    requests whose host is api.telegram.org are refused; every other URL behaves as before. The
    telethon (MTProto) send methods are refused too. The live-session tests in
    test_telegram_integration.py only connect and read, so they are unaffected.
    """
    import urllib.request

    original_post = httpx.post
    original_client_post = httpx.Client.post
    original_client_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send
    original_urlopen = urllib.request.urlopen

    def guarded_post(url, *args, **kwargs):
        if _is_telegram_url(url):
            raise _telegram_refused(url)
        return original_post(url, *args, **kwargs)

    def guarded_client_post(self, url, *args, **kwargs):
        if _is_telegram_url(url):
            raise _telegram_refused(url)
        return original_client_post(self, url, *args, **kwargs)

    def guarded_client_send(self, request, *args, **kwargs):
        if _is_telegram_url(request.url):
            raise _telegram_refused(request.url)
        return original_client_send(self, request, *args, **kwargs)

    async def guarded_async_send(self, request, *args, **kwargs):
        if _is_telegram_url(request.url):
            raise _telegram_refused(request.url)
        return await original_async_send(self, request, *args, **kwargs)

    def guarded_urlopen(url, *args, **kwargs):
        if _is_telegram_url(url):
            raise _telegram_refused(getattr(url, "full_url", url))
        return original_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(httpx, "post", guarded_post)
    monkeypatch.setattr(httpx.Client, "post", guarded_client_post)
    monkeypatch.setattr(httpx.Client, "send", guarded_client_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_send)
    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)

    async def refused_telethon_send(self, *args, **kwargs):
        raise _telegram_refused("telethon TelegramClient send")

    try:
        from telethon import TelegramClient
    except ImportError:  # pragma: no cover - telethon is a project dependency
        return
    monkeypatch.setattr(TelegramClient, "send_message", refused_telethon_send)
    monkeypatch.setattr(TelegramClient, "send_file", refused_telethon_send)


@pytest.fixture(autouse=True)
def _isolate_breakingtrade_alert_state(tmp_path, monkeypatch):
    """BreakingTrade alert rows and the critical-alert latch must not outlive or escape a test.

    alerts.record() writes through store._DB_PATH, so a test that did not redirect it (the
    poller-loop tests) wrote its alert rows into the real signal_engine/data/breakingtrade.db.
    Tests that redirect it themselves override this later in the same test.

    critical.notifier is a module-level object that other modules reach as critical.notifier, so
    its dicts are cleared in place (not replaced): the once-per-day latch otherwise leaks from
    one test into the next and decides whether a later test's banner is raised.
    """
    from signal_engine.analysis.breakingtrade import critical, store

    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    critical.notifier._last_sent.clear()
    critical.notifier._open.clear()
    yield
    critical.notifier._last_sent.clear()
    critical.notifier._open.clear()
