"""Shared test fixtures and factories for signal engine tests."""

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
    """
    monkeypatch.setattr("signal_engine.db._DB_PATH", str(tmp_path / "trades.db"))
