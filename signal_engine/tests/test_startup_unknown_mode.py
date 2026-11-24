"""I-03: an OpenAlgo mode the engine could not read is not "live".

api_client.fetch_trading_mode returns ("unknown", False) on any error. _run_engine used to read
only the boolean, so an unreachable OpenAlgo at startup configured the whole engine as live:
live risk limits, the live risk_store row, and live channel routing.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import startup


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(startup, "_MODE_RETRY_SPACING_S", 0, raising=False)


def _patches(fetch, apply_mode):
    """Everything _run_engine touches around the mode check, with no real side effects."""
    return [
        patch.object(startup, "run_startup_health_checks", AsyncMock(return_value=object())),
        patch.object(startup, "runner_config_ok", AsyncMock(return_value=True)),
        patch.object(startup, "fetch_trading_mode", fetch),
        patch.object(startup, "apply_trade_mode", apply_mode),
        patch.object(startup.mode_guard, "set_startup_phase", MagicMock()),
        patch.object(startup, "set_trade_mode", MagicMock()),
        patch.object(startup, "set_log_mode", MagicMock()),
        patch.object(startup, "reconcile_open_positions", AsyncMock()),
        patch.object(startup, "fetch_available_capital", AsyncMock(return_value=0.0)),
        patch.object(startup, "_start_time_exit_scheduler", MagicMock(return_value=(None, None))),
        patch.object(startup, "_start_runner_service", AsyncMock(return_value=None)),
        patch.object(startup, "_serve_until_shutdown", AsyncMock()),
        patch.object(startup, "_close_resources", AsyncMock()),
    ]


async def _run(fetch):
    apply_mode = MagicMock()
    tracker = MagicMock()
    tracker.start = AsyncMock()
    patches = _patches(fetch, apply_mode)
    for p in patches:
        p.start()
    try:
        await asyncio.wait_for(startup._run_engine(MagicMock(), tracker, AsyncMock()), 10)
        raised = None
    except SystemExit as exc:
        raised = exc
    finally:
        for p in patches:
            p.stop()
    return apply_mode, fetch, raised


@pytest.mark.asyncio
async def test_unknown_then_analyze_starts_as_analyze():
    fetch = AsyncMock(side_effect=[("unknown", False), ("unknown", False), ("analyze", True)])
    apply_mode, _, raised = await _run(fetch)
    assert raised is None
    assert fetch.await_count == 3
    assert [c.args[1] for c in apply_mode.call_args_list] == ["analyze"]


@pytest.mark.asyncio
async def test_definite_live_answer_starts_as_live_without_retry():
    fetch = AsyncMock(return_value=("live", False))
    apply_mode, _, raised = await _run(fetch)
    assert raised is None
    assert fetch.await_count == 1
    assert [c.args[1] for c in apply_mode.call_args_list] == ["live"]


@pytest.mark.asyncio
async def test_always_unknown_aborts_and_never_configures_live(logged):
    fetch = AsyncMock(return_value=("unknown", False))
    apply_mode, _, raised = await _run(fetch)
    assert isinstance(raised, SystemExit) and raised.code == 1
    assert fetch.await_count == 6  # the first read and 5 retries
    live_calls = [c for c in apply_mode.call_args_list if "live" in c.args]
    assert live_calls == []
    apply_mode.assert_not_called()
    assert logged.has("could not confirm whether OpenAlgo is in live or sandbox mode", "CRITICAL")
