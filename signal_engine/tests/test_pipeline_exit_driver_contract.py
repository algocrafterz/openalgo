"""Every exit driver the service can build must offer what the shared cycle calls on it."""

import asyncio
import re
from pathlib import Path

import pandas as pd
import pytest

from signal_engine.pipeline import cycle
from signal_engine.pipeline.exit_driver import ExitDriver
from signal_engine.pipeline.trail_driver import TrailExitDriver

DRIVERS = [ExitDriver, TrailExitDriver]


def _methods_cycle_calls() -> set[str]:
    return set(re.findall(r"deps\.exits\.(\w+)\(", Path(cycle.__file__).read_text()))


@pytest.mark.parametrize("driver", DRIVERS)
def test_driver_has_every_method_the_cycle_calls(driver):
    called = _methods_cycle_calls()
    assert {"catch_up", "on_bar", "holds"} <= called
    missing = [name for name in called if not callable(getattr(driver, name, None))]
    assert not missing, f"{driver.__name__} lacks {missing}: every symbol would fail to evaluate"


def test_trail_catch_up_returns_no_signals():
    driver = TrailExitDriver.__new__(TrailExitDriver)
    ts = pd.Timestamp("2026-11-13 09:20", tz="Asia/Kolkata")
    assert asyncio.run(driver.catch_up("TCS", pd.DataFrame(), ts)) == []
