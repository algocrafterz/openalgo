"""E-18: a new intraday entry is declined once the time exit has started or passed."""

from datetime import datetime
from unittest.mock import patch

import pytest

from signal_engine.main import _handle_entry
from signal_engine.tests.test_main_characterization import (
    _entry_signal,
    _settings_stub,
    harness,
)
from signal_engine.timeutils import IST


def _at(hour, minute):
    return patch(
        "signal_engine.main._now_ist",
        return_value=datetime(2026, 11, 12, hour, minute, 0, tzinfo=IST),
    )


def _cfg(**kw):
    return _settings_stub(time_exit_enabled=True, time_exit_hour=14, time_exit_minute=45, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute", [(14, 45), (14, 59), (15, 10)])
async def test_mis_entry_at_or_after_the_boundary_is_declined(hour, minute):
    h = harness(settings=_cfg())
    with h.stack, _at(hour, minute), patch("signal_engine.main.save_declined") as declined:
        await _handle_entry(_entry_signal())
    h.send_order.assert_not_awaited()
    assert declined.call_args.kwargs["stage"] == "time_exit"


@pytest.mark.asyncio
async def test_mis_entry_before_the_boundary_is_not_declined_by_this_gate():
    h = harness(settings=_cfg())
    with h.stack, _at(14, 44), patch("signal_engine.main.save_declined") as declined:
        await _handle_entry(_entry_signal())
    assert not any(c.kwargs.get("stage") == "time_exit" for c in declined.call_args_list)
    h.send_order.assert_awaited()


@pytest.mark.asyncio
async def test_entry_is_declined_while_the_time_exit_is_running_even_before_the_clock():
    h = harness(settings=_cfg())
    h.tracker._time_exit_active = True
    with h.stack, _at(14, 30), patch("signal_engine.main.save_declined"):
        await _handle_entry(_entry_signal())
    h.send_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_cnc_entry_after_the_boundary_is_untouched():
    h = harness(settings=_cfg())
    with h.stack, _at(15, 10), patch("signal_engine.main.save_declined") as declined:
        await _handle_entry(_entry_signal(product="CNC"))
    assert not any(c.kwargs.get("stage") == "time_exit" for c in declined.call_args_list)


@pytest.mark.asyncio
async def test_gate_is_off_when_the_time_exit_is_disabled():
    h = harness(settings=_settings_stub(time_exit_enabled=False, time_exit_hour=14, time_exit_minute=45))
    with h.stack, _at(15, 10), patch("signal_engine.main.save_declined") as declined:
        await _handle_entry(_entry_signal())
    assert not any(c.kwargs.get("stage") == "time_exit" for c in declined.call_args_list)
