"""BUG-025: the runner start-up readiness check is retried every 5 minutes until entry cutoff."""

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
from loguru import logger

from signal_engine import startup
from signal_engine.pipeline import service
from signal_engine.pipeline.clock import SimClock
from signal_engine.pipeline.service import STRATEGY_FACTORIES
from signal_engine.tests.test_pipeline_service import cfg, start_args, t

DAY = "2026-11-11"


class DatedSource:
    """History endpoint that always has bars (datetime-indexed, as the real source returns)."""

    async def fetch(self, symbols, interval_min, day, now):
        idx = pd.DatetimeIndex([pd.Timestamp(f"{day} 09:15", tz=IST_TZ)])
        return {s: pd.DataFrame({"Close": [1.0]}, index=idx) for s in symbols}


class FlipLoader:
    """Stale (nothing warms) until `fresh_after` calls to load, then fresh. Records calls."""

    def __init__(self, fresh_after=1, boom=None):
        self.fresh_after = fresh_after
        self.boom = boom
        self.calls = 0

    def load(self, symbols, day, s5, s1):
        self.calls += 1
        if self.boom is not None and self.calls <= self.fresh_after:
            raise self.boom
        if self.calls <= self.fresh_after:
            return {}, {}, list(symbols)
        frame = pd.DataFrame({"Close": [1.0]})
        return dict.fromkeys(symbols, frame), dict.fromkeys(symbols, frame), []


def cutoff_min(tag="BREAKOUT"):
    return STRATEGY_FACTORIES[tag]().entry_window_min[1]


def at(minutes):
    return pd.Timestamp(f"{DAY} {minutes // 60:02d}:{minutes % 60:02d}", tz=IST_TZ)


IST_TZ = t("09:00").tz


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0.02)


def capture(level="INFO"):
    lines = []
    hid = logger.add(lambda m: lines.append(str(m)), level=level)
    return lines, hid


class TestRetry:
    def test_stale_store_at_start_then_fresh_starts_one_runner_on_the_retry(self, tmp_path):
        loader = FlipLoader(fresh_after=1)  # first warm-up: stale (the 2026-11-11 case)
        args = start_args(tmp_path, source=DatedSource(), loader=loader, clock=SimClock(t("08:51", DAY)))
        lines, hid = capture()

        async def go():
            svc = await service.start(cfg(), **args)
            assert svc is not None
            await settle()
            runners = [x for x in svc.tasks if x.get_name() == "runner-BREAKOUT"]
            svc.stop()
            return runners

        try:
            runners = asyncio.run(go())
        finally:
            logger.remove(hid)
            args["state"].close()
        assert len(runners) == 1
        assert any("NOT started" in ln and "no symbol warmed up" in ln for ln in lines)
        assert any("started for BREAKOUT" in ln for ln in lines)

    def test_readiness_still_failing_never_starts_a_runner(self, tmp_path):
        loader = FlipLoader(fresh_after=10**6)
        args = start_args(tmp_path, source=DatedSource(), loader=loader, clock=SimClock(t("08:51", DAY)))
        lines, hid = capture()

        async def go():
            svc = await service.start(cfg(), **args)
            await settle(40)
            names = [x.get_name() for x in svc.tasks]
            svc.stop()
            return names

        try:
            names = asyncio.run(go())
        finally:
            logger.remove(hid)
            args["state"].close()
        assert not any(n == "runner-BREAKOUT" for n in names)
        assert not any("started for" in ln for ln in lines)
        assert loader.calls > 2  # it did retry

    def test_retry_stops_at_cutoff_and_logs_once(self, tmp_path):
        loader = FlipLoader(fresh_after=10**6)
        clock = SimClock(at(cutoff_min() - 12))
        args = start_args(tmp_path, source=DatedSource(), loader=loader, clock=clock)
        lines, hid = capture()

        async def go():
            svc = await service.start(cfg(), **args)
            await settle(40)
            done = all(x.done() for x in svc.tasks)
            svc.stop()
            return done

        try:
            done = asyncio.run(go())
        finally:
            logger.remove(hid)
            args["state"].close()
        assert done
        assert loader.calls <= 4  # 5-minute steps within 12 minutes, then no more attempts
        assert sum("no runner starts today" in ln for ln in lines) == 1

    def test_retry_sleeps_300_seconds_between_attempts(self, tmp_path):
        loader = FlipLoader(fresh_after=2)
        clock = SimClock(t("09:00", DAY))
        args = start_args(tmp_path, source=DatedSource(), loader=loader, clock=clock)

        async def go():
            svc = await service.start(cfg(), **args)
            await settle()
            svc.stop()

        try:
            asyncio.run(go())
        finally:
            args["state"].close()
        assert clock.now() - t("09:00", DAY) >= pd.Timedelta(seconds=600)

    def test_a_warmup_exception_is_logged_and_retried_not_raised(self, tmp_path):
        loader = FlipLoader(fresh_after=1, boom=RuntimeError("IO Error: Could not set lock on file"))
        args = start_args(tmp_path, source=DatedSource(), loader=loader, clock=SimClock(t("08:51", DAY)))
        lines, hid = capture()

        async def go():
            svc = await service.start(cfg(), **args)
            await settle()
            n = len([x for x in svc.tasks if x.get_name() == "runner-BREAKOUT"])
            svc.stop()
            return n

        try:
            n = asyncio.run(go())
        finally:
            logger.remove(hid)
            args["state"].close()
        assert n == 1
        assert any("Could not set lock" in ln for ln in lines)

    def test_duplicate_start_race_gives_one_runner(self, tmp_path):
        args = start_args(tmp_path, source=DatedSource(), loader=FlipLoader(fresh_after=1), clock=SimClock(t("08:51", DAY)))

        async def go():
            svc = await service.start(cfg(), **args)
            await settle()
            first = len(svc.tasks)
            # The normal path (or a second retry) asks again for the same strategy.
            assert svc.launch("BREAKOUT") is False
            svc.stop()
            return first

        try:
            asyncio.run(go())
        finally:
            args["state"].close()

    def test_shutdown_cancels_the_retry_task_cleanly(self, tmp_path):
        args = start_args(
            tmp_path, source=DatedSource(), loader=FlipLoader(fresh_after=10**6), clock=SimClock(t("08:51", DAY))
        )

        async def go():
            svc = await service.start(cfg(), **args)
            svc.stop()
            await asyncio.gather(*svc.tasks, return_exceptions=True)
            return svc

        try:
            svc = asyncio.run(go())
        finally:
            args["state"].close()
        assert all(x.done() for x in svc.tasks)


class TestEngineStart:
    @pytest.mark.asyncio
    async def test_a_database_error_in_the_start_check_does_not_abort_the_engine(self):
        lines, hid = capture("ERROR")
        boom = RuntimeError("IO Error: Could not set lock on file ... historify.duckdb")
        try:
            with (
                patch.object(startup.service, "start", AsyncMock(side_effect=boom)),
                patch.object(
                    startup,
                    "settings",
                    SimpleNamespace(
                        runner=None,
                        time_exit_hour=14,
                        time_exit_minute=45,
                        stale_signal_seconds=60,
                        product="MIS",
                    ),
                ),
            ):
                assert await startup._start_runner_service(object(), "analyze", None) is None
        finally:
            logger.remove(hid)
        assert any("could not start" in ln for ln in lines)
