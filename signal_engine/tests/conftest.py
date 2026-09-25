"""Shared test fixtures and factories for signal engine tests."""

import httpx
import pytest

from signal_engine.models import Direction, Signal
from signal_engine.strategies import ORB


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
