"""Start-up wiring for the Python signal runner: config guard, task lifecycle, FD hygiene."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import startup
from signal_engine.config import RunnerSettings, RunnerStrategy
from signal_engine.pipeline import service
from signal_engine.pipeline.single_source import SingleSourceError


def runner_cfg(source="pine", enabled=False, owner="tracker"):
    return RunnerSettings(
        enabled=enabled,
        bar_interval_minutes=5,
        bar_delay_seconds=3.0,
        cycle_deadline_seconds=25.0,
        retry_backoff_seconds=2.0,
        history_timeout_seconds=10.0,
        max_concurrent_requests=8,
        warmup_sessions=20,
        universe=("TCS",),
        strategies={"BREAKOUT": RunnerStrategy(source, "tp1_only", owner)},
    )


class TestRunnerConfigGuard:
    @pytest.mark.asyncio
    async def test_consistent_config_passes_and_logs_sources(self):
        with (
            patch.object(startup, "settings", SimpleNamespace(runner=runner_cfg())),
            patch.object(startup.single_source, "log_active_sources") as log,
        ):
            assert await startup.runner_config_ok() is True
        log.assert_called_once()

    @pytest.mark.asyncio
    async def test_python_source_with_disabled_runner_refuses_and_notifies(self):
        with (
            patch.object(
                startup, "settings", SimpleNamespace(runner=runner_cfg("python", enabled=False))
            ),
            patch.object(startup.notifier, "notify_startup_result", AsyncMock()) as notify,
        ):
            assert await startup.runner_config_ok() is False
        notify.assert_awaited_once()
        assert notify.await_args.kwargs["all_passed"] is False

    @pytest.mark.asyncio
    async def test_ladder_owner_with_pine_source_refuses(self):
        with (
            patch.object(
                startup, "settings", SimpleNamespace(runner=runner_cfg("pine", owner="ladder"))
            ),
            patch.object(startup.notifier, "notify_startup_result", AsyncMock()),
        ):
            assert await startup.runner_config_ok() is False


class TestStartRunnerService:
    @pytest.mark.asyncio
    async def test_delegates_to_service_start_with_engine_settings(self):
        sentinel = object()
        st = SimpleNamespace(
            runner=runner_cfg(enabled=True),
            time_exit_hour=14,
            time_exit_minute=45,
            stale_signal_seconds=60,
            product="MIS",
        )
        dispatch = AsyncMock()
        with (
            patch.object(startup, "settings", st),
            patch.object(service, "start", AsyncMock(return_value=sentinel)) as start,
        ):
            got = await startup._start_runner_service(MagicMock(), "analyze", dispatch)
        assert got is sentinel
        kw = start.await_args.kwargs
        assert kw["dispatch"] is dispatch and kw["trade_mode"] == "analyze"
        assert kw["time_exit"] == (14, 45) and kw["stale_limit_s"] == 60 and kw["product"] == "MIS"

    @pytest.mark.asyncio
    async def test_start_errors_are_contained_and_logged(self):
        st = SimpleNamespace(
            runner=runner_cfg(enabled=True),
            time_exit_hour=14,
            time_exit_minute=45,
            stale_signal_seconds=60,
            product="MIS",
        )
        for err in (service.RunnerStartError("no symbols"), SingleSourceError("no dispatcher")):
            with (
                patch.object(startup, "settings", st),
                patch.object(service, "start", AsyncMock(side_effect=err)),
            ):
                assert await startup._start_runner_service(MagicMock(), "analyze", None) is None


class TestCloseResources:
    @pytest.mark.asyncio
    async def test_runner_state_store_is_closed_on_shutdown(self):
        svc = MagicMock()
        risk_engine = MagicMock()
        risk_engine._store = None
        await startup._close_resources(risk_engine, svc)
        svc.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_runner_that_cannot_close_does_not_break_shutdown(self):
        svc = MagicMock()
        svc.close.side_effect = RuntimeError("gone")
        risk_engine = MagicMock()
        risk_engine._store = None
        await startup._close_resources(risk_engine, svc)


def _engine_patches(stack, *, cfg_ok=True, service_obj=None, serve=None):
    tracker = MagicMock()
    tracker.start = AsyncMock()
    stack.enter_context(
        patch.object(startup, "run_startup_health_checks", AsyncMock(return_value=MagicMock()))
    )
    stack.enter_context(
        patch.object(startup, "fetch_trading_mode", AsyncMock(return_value=("analyze", True)))
    )
    for name in ("apply_trade_mode", "set_trade_mode", "set_log_mode"):
        stack.enter_context(patch.object(startup, name, MagicMock()))
    stack.enter_context(patch.object(startup.mode_guard, "set_startup_phase", MagicMock()))
    stack.enter_context(patch.object(startup, "reconcile_open_positions", AsyncMock()))
    stack.enter_context(patch.object(startup, "fetch_available_capital", AsyncMock(return_value=0)))
    stack.enter_context(patch.object(startup.notifier, "notify_startup_summary", AsyncMock()))
    stack.enter_context(patch.object(startup, "_resolve_broker_name", MagicMock(return_value="x")))
    stack.enter_context(
        patch.object(startup, "_start_time_exit_scheduler", MagicMock(return_value=(None, None)))
    )
    stack.enter_context(patch.object(startup, "runner_config_ok", AsyncMock(return_value=cfg_ok)))
    stack.enter_context(
        patch.object(startup, "_start_runner_service", AsyncMock(return_value=service_obj))
    )
    stack.enter_context(patch.object(startup, "_serve_until_shutdown", serve or AsyncMock()))
    close = stack.enter_context(patch.object(startup, "_close_resources", AsyncMock()))
    return tracker, close


class TestRunEngineWiring:
    def test_runner_task_is_created_and_cancelled_and_resources_closed(self):
        from contextlib import ExitStack

        svc = MagicMock()
        dispatch = AsyncMock()
        with ExitStack() as stack:
            tracker, close = _engine_patches(stack, service_obj=svc)
            asyncio.run(startup._run_engine(MagicMock(), tracker, AsyncMock(), dispatch))
            started = startup._start_runner_service
        started.assert_awaited_once()
        assert started.await_args.args[2] is dispatch and started.await_args.args[1] == "analyze"
        svc.cancel.assert_called_once()
        assert close.await_args.args[1] is svc

    def test_inconsistent_runner_config_aborts_before_anything_starts(self):
        from contextlib import ExitStack

        with ExitStack() as stack:
            tracker, close = _engine_patches(stack, cfg_ok=False)
            asyncio.run(startup._run_engine(MagicMock(), tracker, AsyncMock(), AsyncMock()))
            started = startup._start_runner_service
        tracker.start.assert_not_called()
        started.assert_not_awaited()

    def test_no_runner_service_is_fine(self):
        from contextlib import ExitStack

        with ExitStack() as stack:
            tracker, close = _engine_patches(stack, service_obj=None)
            asyncio.run(startup._run_engine(MagicMock(), tracker, AsyncMock()))
        assert close.await_args.args[1] is None

    def test_runner_is_cancelled_even_when_serving_raises(self):
        from contextlib import ExitStack

        svc = MagicMock()
        with ExitStack() as stack:
            tracker, _ = _engine_patches(
                stack, service_obj=svc, serve=AsyncMock(side_effect=RuntimeError("x"))
            )
            with pytest.raises(RuntimeError):
                asyncio.run(startup._run_engine(MagicMock(), tracker, AsyncMock(), AsyncMock()))
        svc.cancel.assert_called_once()
