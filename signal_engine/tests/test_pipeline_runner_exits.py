"""The runner keeps managing open positions after the entry window closes."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from signal_engine.models import Direction
from signal_engine.pipeline.bars import ReplaySource
from signal_engine.pipeline.clock import SimClock
from signal_engine.pipeline.context import RollingContext
from signal_engine.pipeline.runner import Runner
from signal_engine.pipeline.sink import RecordingSink
from signal_engine.pipeline.state import RunnerStateStore

from .test_pipeline_cycle import DAY, Loader, ScriptedStrategy, bars, cfg, t
from .test_pipeline_cycle_exits import BarSink, driver, held
from .test_pipeline_runner import DAY_TIMES


def build(tmp_path, exits, manage_until_min=None, hit_at="12:10"):
    frames = {"AAA": bars(DAY_TIMES)}
    if hit_at:
        frames["AAA"].loc[t(hit_at), "High"] = 102.0
    strat = ScriptedStrategy({})
    st = RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: False)
    runner = Runner(
        clock=SimClock(t("09:00")),
        source=ReplaySource(frames),
        sink=BarSink(),
        strategy=strat,
        context=RollingContext(strat, Loader()),
        state=st,
        cfg=cfg(),
        symbols=["AAA"],
        mode="analyze",
        product="MIS",
        stale_limit_s=60.0,
        halted=lambda: False,
        price_band=(50.0, 1500.0),
        executor=ThreadPoolExecutor(2),
        exits=exits,
        manage_until_min=manage_until_min,
    )
    return runner, st


class TestManageAfterWindow:
    def test_exit_after_the_entry_cutoff_is_submitted(self, tmp_path):
        runner, st = build(tmp_path, driver(held()), manage_until_min=14 * 60 + 45)
        asyncio.run(runner.run(DAY))
        st.close()
        exits = [s for s in runner.sink.signals if s.direction == Direction.EXIT]
        assert [(s.symbol, s.tp_level) for s in exits] == [("AAA", "TP1")]

    def test_without_manage_until_the_runner_stops_at_the_cutoff(self, tmp_path):
        runner, st = build(tmp_path, driver(held()))
        asyncio.run(runner.run(DAY))
        st.close()
        assert runner.sink.signals == []
        assert runner.clock.now() <= t("11:50:00")

    def test_last_cycle_is_the_bar_before_manage_until(self, tmp_path):
        runner, st = build(tmp_path, driver(held()), manage_until_min=12 * 60 + 30, hit_at=None)
        asyncio.run(runner.run(DAY))
        st.close()
        assert max(r.bar_close for r in runner.reports) == t("12:30")

    def test_no_open_position_means_cheap_empty_cycles(self, tmp_path):
        runner, st = build(tmp_path, driver(), manage_until_min=12 * 60 + 30, hit_at=None)
        asyncio.run(runner.run(DAY))
        st.close()
        late = [r for r in runner.reports if r.bar_close > t("11:50")]
        assert late and all(r.evaluated == 0 for r in late)
