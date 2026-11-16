"""BUG-025: the runner start-up readiness check is retried every 5 minutes until entry cutoff."""

import asyncio
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
        """A passing retry and a manual launch race: exactly one runner task results."""
        args = start_args(
            tmp_path, source=DatedSource(), loader=FlipLoader(fresh_after=1), clock=SimClock(t("08:51", DAY))
        )

        async def go():
            svc = await service.start(cfg(), **args)

            async def manual():
                for _ in range(40):
                    await asyncio.sleep(0.005)
                    svc.launch("BREAKOUT")

            await asyncio.gather(manual(), settle())
            runners = [x for x in svc.tasks if x.get_name() == "runner-BREAKOUT"]
            svc.stop()
            return runners

        try:
            runners = asyncio.run(go())
        finally:
            args["state"].close()
        assert len(runners) == 1

    def test_two_concurrent_launch_calls_give_one_task(self):
        async def go():
            svc = service.RunnerService(
                launchers={"BREAKOUT": lambda: asyncio.create_task(asyncio.sleep(0))}
            )

            async def call():
                return svc.launch("BREAKOUT")

            res = await asyncio.gather(call(), call())
            await asyncio.gather(*svc.tasks)
            return res, svc.tasks

        res, tasks = asyncio.run(go())
        assert sorted(res) == [False, True] and len(tasks) == 1

    def test_cutoff_computation_failure_is_critical_not_silent(self, tmp_path):
        args = start_args(
            tmp_path, source=DatedSource(), loader=FlipLoader(fresh_after=10**6), clock=SimClock(t("08:51", DAY))
        )
        lines, hid = capture("CRITICAL")
        real = service.STRATEGY_FACTORIES["BREAKOUT"]
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) > 1:  # the start-up probe works; the retry's cutoff computation fails
                raise RuntimeError("spec file unreadable")
            return real()

        async def go():
            with patch.dict(service.STRATEGY_FACTORIES, {"BREAKOUT": flaky}):
                svc = await service.start(cfg(), **args)
                await settle()
            retry = [x for x in svc.tasks if x.get_name() == "runner-start-retry-BREAKOUT"]
            svc.stop()
            return retry

        try:
            retry = asyncio.run(go())
        finally:
            logger.remove(hid)
            args["state"].close()
        assert retry and retry[0].done()
        assert any("start retry crashed" in ln and "BREAKOUT" in ln for ln in lines)

    def test_done_callback_logs_a_task_that_died_with_an_exception(self):
        lines, hid = capture("CRITICAL")

        async def go():
            async def boom():
                raise ValueError("x")

            task = asyncio.create_task(boom(), name="runner-start-retry-ZZ")
            task.add_done_callback(service._log_retry_outcome)
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        try:
            asyncio.run(go())
        finally:
            logger.remove(hid)
        assert any("runner-start-retry-ZZ" in ln and "ValueError" in ln for ln in lines)

    def test_failing_builder_leaves_the_tag_startable(self):
        calls = []

        def build():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("builder failed")
            return SimpleNamespace(done=lambda: False)

        svc = service.RunnerService(launchers={"BREAKOUT": build})
        lines, hid = capture("ERROR")
        try:
            assert svc.launch("BREAKOUT") is False
            assert "BREAKOUT" not in svc.started and svc.tasks == []
            assert svc.launch("BREAKOUT") is True
            assert svc.launch("BREAKOUT") is False
        finally:
            logger.remove(hid)
        assert any("launch failed" in ln for ln in lines)
        assert len(svc.tasks) == 1

    def test_non_session_day_logs_one_line_and_ends(self, tmp_path):
        args = start_args(
            tmp_path, source=DatedSource(), loader=FlipLoader(fresh_after=10**6), clock=SimClock(t("08:51", DAY))
        )
        lines, hid = capture()

        async def go():
            with patch.object(service, "is_session_day", lambda d: False):
                svc = await service.start(cfg(), **args)
                await settle()
            done = all(x.done() for x in svc.tasks)
            svc.stop()
            return done

        try:
            done = asyncio.run(go())
        finally:
            logger.remove(hid)
            args["state"].close()
        assert done
        assert sum("not a session day" in ln for ln in lines) == 1

    def test_alert_sent_on_late_start_and_on_cutoff_give_up(self, tmp_path):
        sent = []

        async def alert(text):
            sent.append(text)

        late = start_args(
            tmp_path, source=DatedSource(), loader=FlipLoader(fresh_after=1),
            clock=SimClock(t("08:51", DAY)), failure_alert=alert,
        )

        async def go_late():
            svc = await service.start(cfg(), **late)
            await settle()
            svc.stop()

        try:
            asyncio.run(go_late())
        finally:
            late["state"].close()
        assert len(sent) == 1 and "started late" in sent[0]

        sent.clear()
        giveup = start_args(
            tmp_path, source=DatedSource(), loader=FlipLoader(fresh_after=10**6),
            clock=SimClock(at(cutoff_min() - 12)), failure_alert=alert,
        )

        async def go_giveup():
            svc = await service.start(cfg(), **giveup)
            await settle(40)
            svc.stop()

        try:
            asyncio.run(go_giveup())
        finally:
            giveup["state"].close()
        assert len(sent) == 1 and "no runner starts today" in sent[0]

    def test_retry_attempts_are_staggered_per_strategy(self):
        """A strategy at index i first retries i * RETRY_STAGGER_S after index 0."""
        firsts = []

        async def go():
            for index in (0, 1):
                seen = []

                async def fake_preflight(cfg_, src, ctx, syms, now, day, seen=seen):
                    seen.append(now)
                    return False, "stale"

                with patch.object(service, "preflight", fake_preflight):
                    await service._retry_loop(
                        service.RunnerService(), "BREAKOUT", cfg(), None, FlipLoader(),
                        ["TCS"], SimClock(t("08:51", DAY)), index * service.RETRY_STAGGER_S, None,
                    )
                firsts.append(seen[0])

        asyncio.run(go())
        assert firsts[1] - firsts[0] == pd.Timedelta(seconds=service.RETRY_STAGGER_S)

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
