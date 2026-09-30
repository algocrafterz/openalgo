"""Service: builds the runner for a process, health-checks it, and runs one Runner per session."""

import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from loguru import logger

from signal_engine.config import RunnerSettings, RunnerStrategy
from signal_engine.pipeline import service
from signal_engine.pipeline.bars import MissingBarsError
from signal_engine.pipeline.clock import SimClock
from signal_engine.pipeline.exit_driver import ExitDriver
from signal_engine.pipeline.single_source import SingleSourceError
from signal_engine.timeutils import IST


def t(s, day="2026-09-30"):
    return pd.Timestamp(f"{day} {s}", tz=IST)


def cfg(source="python", owner="tracker", enabled=True, universe=("TCS", "INFY")):
    return RunnerSettings(
        enabled=enabled,
        bar_interval_minutes=5,
        bar_delay_seconds=3.0,
        cycle_deadline_seconds=25.0,
        retry_backoff_seconds=2.0,
        history_timeout_seconds=10.0,
        max_concurrent_requests=8,
        warmup_sessions=20,
        universe=universe,
        strategies={"BREAKOUT": RunnerStrategy(source, "tp1_only", owner)},
    )


class FakeSource:
    def __init__(self, ok_days=None, boom=False):
        self.ok_days = ok_days
        self.boom = boom
        self.calls = []

    async def fetch(self, symbols, interval_min, day, now):
        self.calls.append(day)
        if self.boom:
            raise OSError("down")
        if self.ok_days is None or day in self.ok_days:
            return {symbols[0]: pd.DataFrame({"Close": [1.0]})}
        raise MissingBarsError(list(symbols), {})


class FakeContext:
    def __init__(self, warmed=("TCS", "INFY")):
        self._warmed = set(warmed)
        self.failed = set()

    @property
    def warmed(self):
        return self._warmed

    def warm(self, symbols, day):
        return [s for s in symbols if s not in self._warmed]


class TestResolveSymbols:
    def test_explicit_list(self):
        assert service.resolve_symbols(cfg(universe=("TCS", "INFY")), lambda: ["X"]) == [
            "TCS",
            "INFY",
        ]

    def test_fno_all_uses_the_historify_listing(self):
        assert service.resolve_symbols(cfg(universe="fno_all"), lambda: ["A", "B"]) == ["A", "B"]

    def test_empty_universe_is_an_error(self):
        with pytest.raises(service.RunnerStartError, match="universe"):
            service.resolve_symbols(cfg(universe="fno_all"), lambda: [])


class TestHistoryReachable:
    def test_reachable_when_a_recent_weekday_has_bars(self):
        src = FakeSource()
        assert asyncio.run(service.history_reachable(src, "TCS", t("10:00")))

    def test_walks_back_over_a_holiday_or_weekend(self):
        # 2026-09-30 is a Wednesday; only Friday 2026-09-25 has bars
        src = FakeSource(ok_days={dt.date(2026, 9, 25)})
        assert asyncio.run(service.history_reachable(src, "TCS", t("10:00")))
        assert dt.date(2026, 9, 26) not in src.calls  # Saturday is never probed

    def test_unreachable_when_no_recent_day_has_bars(self):
        assert not asyncio.run(
            service.history_reachable(FakeSource(ok_days=set()), "TCS", t("10:00"))
        )

    def test_transport_errors_are_unreachable_not_raised(self):
        assert not asyncio.run(service.history_reachable(FakeSource(boom=True), "TCS", t("10:00")))


class TestBuildSink:
    def test_python_source_dispatches(self):
        sink = service.build_sink(cfg("python"), "BREAKOUT", AsyncMock())
        assert sink.dispatches is True

    def test_pine_source_is_shadow(self):
        sink = service.build_sink(cfg("pine"), "BREAKOUT", AsyncMock())
        assert sink.dispatches is False

    def test_python_source_without_a_dispatcher_refuses(self):
        with pytest.raises(SingleSourceError):
            service.build_sink(cfg("python"), "BREAKOUT", None)


class TestBuildExits:
    def test_ladder_owner_gets_a_driver(self):
        d = service.build_exits(
            cfg("python", "ladder"), "BREAKOUT", positions=object(), broker_qty=AsyncMock()
        )
        assert isinstance(d, ExitDriver)

    def test_tracker_owner_gets_none(self):
        assert (
            service.build_exits(cfg("python", "tracker"), "BREAKOUT", object(), AsyncMock()) is None
        )

    def test_shadow_never_gets_a_driver(self):
        assert (
            service.build_exits(cfg("pine", "tracker"), "BREAKOUT", object(), AsyncMock()) is None
        )


class TestManageUntil:
    def test_last_stepped_bar_is_the_one_before_the_time_exit(self):
        assert service.manage_until_min(14, 45) == 14 * 60 + 45


class TestPreflight:
    def test_disabled_runner_is_not_started(self):
        ok, why = asyncio.run(
            service.preflight(
                cfg(enabled=False),
                FakeSource(),
                FakeContext(),
                ["TCS"],
                t("09:00"),
                t("09:00").date(),
            )
        )
        assert not ok and "disabled" in why

    def test_history_unreachable_fails_closed(self):
        ok, why = asyncio.run(
            service.preflight(
                cfg(),
                FakeSource(ok_days=set()),
                FakeContext(),
                ["TCS"],
                t("09:00"),
                t("09:00").date(),
            )
        )
        assert not ok and "history" in why

    def test_no_symbol_warmed_fails_closed(self):
        ok, why = asyncio.run(
            service.preflight(
                cfg(), FakeSource(), FakeContext(warmed=()), ["TCS"], t("09:00"), t("09:00").date()
            )
        )
        assert not ok and "warm-up" in why

    def test_healthy_start_logs_both_checks(self):
        lines = []
        hid = logger.add(lambda m: lines.append(str(m)))
        ok, _ = asyncio.run(
            service.preflight(
                cfg(), FakeSource(), FakeContext(), ["TCS", "INFY"], t("09:00"), t("09:00").date()
            )
        )
        logger.remove(hid)
        assert ok
        assert any("history reachable" in ln for ln in lines)
        assert any("runner warm-up finished" in ln for ln in lines)


class TestRunDays:
    def test_one_runner_per_weekday_and_weekends_are_skipped(self):
        ran = []

        class R:
            def __init__(self, day):
                self.day = day

            async def run(self, day):
                ran.append(day)

        clock = SimClock(t("09:00", "2026-09-25"))  # a Friday

        async def go():
            task = asyncio.create_task(service.run_days(lambda d: R(d), clock))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())
        days = sorted(set(ran))
        assert dt.date(2026, 9, 25) in days
        assert dt.date(2026, 9, 26) not in days and dt.date(2026, 9, 27) not in days
        assert dt.date(2026, 9, 28) in days

    def test_a_failing_session_does_not_end_the_loop(self):
        calls = []

        class R:
            def __init__(self, day):
                self.day = day

            async def run(self, day):
                calls.append(day)
                if len(calls) == 1:
                    raise RuntimeError("bad day")

        clock = SimClock(t("09:00", "2026-09-28"))

        async def go():
            task = asyncio.create_task(service.run_days(lambda d: R(d), clock))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())
        assert len(calls) >= 2


class Loader:
    def load(self, symbols, day, s5, s1):
        frame = pd.DataFrame({"Close": [1.0]})
        return dict.fromkeys(symbols, frame), dict.fromkeys(symbols, frame), []


class FailLoader:
    def load(self, symbols, day, s5, s1):
        return {}, {}, list(symbols)


def start_args(tmp_path, **over):
    from signal_engine.pipeline.state import RunnerStateStore

    args = {
        "dispatch": AsyncMock(),
        "tracker": object(),
        "trade_mode": "analyze",
        "time_exit": (14, 45),
        "halted": lambda: False,
        "stale_limit_s": 60.0,
        "product": "MIS",
        "clock": SimClock(t("09:00")),
        "source": FakeSource(),
        "loader": Loader(),
        "historify_symbols": lambda: ["TCS"],
        "state": RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda _s: False),
    }
    args.update(over)
    return args


class TestStart:
    def test_disabled_returns_none_and_touches_nothing(self, tmp_path):
        args = start_args(tmp_path)
        assert asyncio.run(service.start(cfg(enabled=False), **args)) is None
        args["state"].close()

    def test_unknown_strategy_tag_refuses(self, tmp_path):
        bad = cfg()
        bad = RunnerSettings(
            **{**bad.__dict__, "strategies": {"ZZZ": RunnerStrategy("pine", "tp1_only")}}
        )
        args = start_args(tmp_path)
        with pytest.raises(service.RunnerStartError, match="ZZZ"):
            asyncio.run(service.start(bad, **args))
        args["state"].close()

    def test_unreachable_history_fails_closed_and_logs_critical(self, tmp_path):
        lines = []
        hid = logger.add(lambda m: lines.append(str(m)), level="CRITICAL")
        args = start_args(tmp_path, source=FakeSource(ok_days=set()))
        assert asyncio.run(service.start(cfg(), **args)) is None
        logger.remove(hid)
        args["state"].close()
        assert any("NOT started" in ln for ln in lines)

    def test_failed_warmup_fails_closed(self, tmp_path):
        args = start_args(tmp_path, loader=FailLoader())
        assert asyncio.run(service.start(cfg(), **args)) is None
        args["state"].close()

    def test_start_then_stop_cancels_tasks_and_closes_the_state_store(self, tmp_path):
        args = start_args(tmp_path)

        async def go():
            svc = await service.start(cfg(), **args)
            assert svc is not None and len(svc.tasks) == 1
            task = svc.tasks[0]
            svc.stop()
            with pytest.raises(asyncio.CancelledError):
                await task
            return svc

        svc = asyncio.run(go())
        assert svc.state is None
        assert args["state"]._conn is None

    def test_python_source_without_dispatcher_fails_in_the_session_not_silently(self, tmp_path):
        """build_sink refuses when a python strategy has no dispatcher; the loop logs and lives."""
        args = start_args(tmp_path, dispatch=None)
        lines = []
        hid = logger.add(lambda m: lines.append(str(m)), level="ERROR")

        async def go():
            svc = await service.start(cfg("python"), **args)
            await asyncio.sleep(0.05)
            svc.stop()

        asyncio.run(go())
        logger.remove(hid)
        assert any("no dispatcher" in ln for ln in lines)
