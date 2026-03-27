"""Shared test fixtures and factories for signal engine tests."""

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
