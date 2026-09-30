"""Runner lifecycle over a simulated day: one path, exactly-once, restart-safe."""

import asyncio
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from signal_engine.pipeline.bars import ReplaySource
from signal_engine.pipeline.clock import SimClock
from signal_engine.pipeline.context import RollingContext
from signal_engine.pipeline.runner import Runner
from signal_engine.pipeline.single_source import SingleSourceError
from signal_engine.pipeline.sink import RecordingSink
from signal_engine.pipeline.state import RunnerStateStore

from .test_pipeline_cycle import DAY, Loader, ScriptedStrategy, bars, cfg, fire, t

DAY_TIMES = [f"{h:02d}:{m:02d}" for h in range(9, 13) for m in range(0, 60, 5) if (h, m) >= (9, 15)]


def make_runner(
    tmp_path, script, start="09:00", sink=None, state=None, source_kind="python", symbols=("AAA",)
):
    strat = ScriptedStrategy(script)
    frames = {s: bars(DAY_TIMES) for s in symbols}
    st = state or RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: False)
    c = cfg()
    if source_kind != "python":
        from signal_engine.config import RunnerStrategy

        c = type(c)(
            **{**c.__dict__, "strategies": {"BREAKOUT": RunnerStrategy(source_kind, "tp1_only")}}
        )
    runner = Runner(
        clock=SimClock(t(start)),
        source=ReplaySource(frames),
        sink=sink or RecordingSink(),
        strategy=strat,
        context=RollingContext(strat, Loader()),
        state=st,
        cfg=c,
        symbols=list(symbols),
        mode="analyze",
        product="MIS",
        stale_limit_s=60.0,
        halted=lambda: False,
        price_band=(50.0, 1500.0),
        executor=ThreadPoolExecutor(2),
    )
    return runner, st


class TestRun:
    def test_fires_once_each_across_the_day(self, tmp_path):
        script = {"AAA": [fire(at="09:55"), fire(at="10:20", price=101.0)]}
        runner, st = make_runner(tmp_path, script)
        asyncio.run(runner.run(DAY))
        st.close()
        assert [s.sig_id for s in runner.sink.signals] == ["AAA-20260930-0955", "AAA-20260930-1020"]

    def test_stops_after_the_entry_window_cutoff(self, tmp_path):
        script = {"AAA": [fire(at="11:50")]}
        runner, st = make_runner(tmp_path, script)
        asyncio.run(runner.run(DAY))
        st.close()
        assert runner.sink.signals == []
        assert runner.clock.now() <= t("11:50:00")

    def test_no_cycle_runs_before_the_window_opens(self, tmp_path):
        runner, st = make_runner(tmp_path, {})
        asyncio.run(runner.run(DAY))
        st.close()
        assert min(r.bar_close for r in runner.reports) >= t("09:50")

    def test_mid_day_restart_neither_refires_nor_skips(self, tmp_path):
        script = {"AAA": [fire(at="09:55"), fire(at="10:20", price=101.0)]}
        first, st = make_runner(tmp_path, script, start="09:00")
        first.stop_after = t("10:10")
        asyncio.run(first.run(DAY))
        second, _ = make_runner(tmp_path, script, start="10:11", state=st)
        asyncio.run(second.run(DAY))
        st.close()
        got = [s.sig_id for s in first.sink.signals + second.sink.signals]
        assert got == ["AAA-20260930-0955", "AAA-20260930-1020"]

    def test_fires_are_collected_before_gates_for_parity(self, tmp_path):
        script = {"AAA": [fire(at="09:55", price=2000.0)]}  # ineligible, still a Fire
        runner, st = make_runner(tmp_path, script)
        asyncio.run(runner.run(DAY))
        st.close()
        assert runner.sink.signals == []
        assert [f.bar_time for f in runner.fires] == [t("09:55")]

    def test_warmup_summary_is_logged(self, tmp_path):
        from loguru import logger

        lines = []
        hid = logger.add(lambda m: lines.append(str(m)))
        runner, st = make_runner(tmp_path, {})
        asyncio.run(runner.run(DAY))
        st.close()
        logger.remove(hid)
        assert any("warm-up finished" in ln for ln in lines)


class TestSingleSourceGuard:
    class DispatchingSink(RecordingSink):
        dispatches = True

    def test_pine_strategy_refuses_a_dispatching_sink(self, tmp_path):
        runner, st = make_runner(tmp_path, {}, sink=self.DispatchingSink(), source_kind="pine")
        with pytest.raises(SingleSourceError):
            asyncio.run(runner.run(DAY))
        st.close()

    def test_pine_strategy_may_run_in_shadow(self, tmp_path):
        runner, st = make_runner(tmp_path, {}, source_kind="pine")
        asyncio.run(runner.run(DAY))
        st.close()

    def test_python_strategy_may_dispatch(self, tmp_path):
        runner, st = make_runner(tmp_path, {}, sink=self.DispatchingSink())
        asyncio.run(runner.run(DAY))
        st.close()
