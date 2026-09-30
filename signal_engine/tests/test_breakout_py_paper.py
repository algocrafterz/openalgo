"""BREAKOUT-PY: the Python runner's paper-only strategy tag, running beside the Pine BREAKOUT.

Two tags, one strategy core. The Pine tag is untouched; the Python tag dispatches only into
OpenAlgo's ANALYZE (sandbox) mode and is refused, loudly, anywhere else.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from loguru import logger

from signal_engine import mode_guard
from signal_engine.config import RunnerSettings, RunnerStrategy, settings
from signal_engine.models import Direction, Signal
from signal_engine.pipeline import service, single_source
from signal_engine.pipeline.cycle import CycleDeps, run_cycle
from signal_engine.pipeline.exit_driver import ExitDriver, plan_for
from signal_engine.pipeline.signals import fire_to_signal, sig_id
from signal_engine.pipeline.sink import DispatchSink
from signal_engine.pipeline.state import RunnerStateStore
from signal_engine.pipeline.strategies.breakout import BreakoutStrategy
from signal_engine.strategies import BREAKOUT, BREAKOUT_PY, REGISTRY, channel_base, channelled
from signal_engine.tests.test_pipeline_cycle import (
    ALL_TIMES,
    DAY,
    Loader,
    ScriptedStrategy,
    bars,
    fire,
    t,
)
from signal_engine.timeutils import IST

PY = "BREAKOUT-PY"


@pytest.fixture
def logs():
    lines = []
    hid = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    yield lines
    logger.remove(hid)


@pytest.fixture(autouse=True)
def _clean_mode_guard():
    mode_guard.reset()
    yield
    mode_guard.reset()


def _sig(strategy=PY, symbol="TCS"):
    return Signal(
        strategy=strategy,
        direction=Direction.LONG,
        symbol=symbol,
        entry=100.0,
        sl=99.0,
        tp=102.0,
        sig_id=f"{symbol}-20260930-0955",
    )


# ---- registry and tag ------------------------------------------------------------------------


class TestRegistry:
    def test_python_tag_is_registered_paper_only_without_a_channel(self):
        meta = REGISTRY[PY]
        assert BREAKOUT_PY == PY
        assert meta.channel_base is None
        assert meta.tradeable is False
        assert meta.note  # the why is written down, not implied

    def test_python_tag_is_not_a_channelled_strategy(self):
        assert PY not in channelled()
        assert channel_base(PY) is None

    def test_pine_tag_registration_is_unchanged(self):
        meta = REGISTRY[BREAKOUT]
        assert (meta.channel_base, meta.tradeable) == ("intraday-breakout", True)


class TestStrategyName:
    def test_default_name_is_the_pine_tag(self):
        assert BreakoutStrategy().name == "BREAKOUT"

    def test_name_is_injectable(self):
        assert BreakoutStrategy(name=PY).name == PY

    def test_service_factories_cover_both_tags_on_one_core(self):
        assert service.STRATEGY_FACTORIES["BREAKOUT"]().name == "BREAKOUT"
        py = service.STRATEGY_FACTORIES[PY]()
        assert py.name == PY
        assert isinstance(py, BreakoutStrategy)


# ---- sig_id and signal mapping ---------------------------------------------------------------


class TestSigId:
    BAR = pd.Timestamp("2026-09-30 09:55", tz=IST)

    def test_pine_shaped_key_is_kept_for_the_default_tag(self):
        assert sig_id("TCS", self.BAR) == "TCS-20260930-0955"
        assert sig_id("TCS", self.BAR, "BREAKOUT") == "TCS-20260930-0955"

    def test_other_tags_are_prefixed_so_they_never_collide_with_pine(self):
        assert sig_id("TCS", self.BAR, PY) == "BREAKOUT-PY-TCS-20260930-0955"

    def test_fire_to_signal_carries_the_tag_everywhere(self):
        sig = fire_to_signal(fire("TCS", "09:55"), "MIS", strategy=PY)
        assert sig.strategy == PY
        assert sig.sig_id == "BREAKOUT-PY-TCS-20260930-0955"
        assert sig.context["trigger"] == "PDH-RT"

    def test_fire_to_signal_default_is_unchanged_for_pine(self):
        sig = fire_to_signal(fire("TCS", "09:55"), "MIS")
        assert (sig.strategy, sig.sig_id) == ("BREAKOUT", "TCS-20260930-0955")


class _Named(ScriptedStrategy):
    def __init__(self, name, script):
        super().__init__(script)
        self.name = name


def _deps(tmp_path, name, sink, pine_sig_ids=()):
    strat = _Named(name, {"AAA": [fire("AAA", "10:00")]})
    store = RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: s in pine_sig_ids)
    from signal_engine.pipeline.bars import ReplaySource
    from signal_engine.pipeline.clock import SimClock
    from signal_engine.pipeline.context import RollingContext

    return CycleDeps(
        strategy=strat,
        source=ReplaySource({"AAA": bars(ALL_TIMES)}),
        context=RollingContext(strat, Loader()),
        sink=sink,
        state=store,
        clock=SimClock(t("10:05:03")),
        symbols=["AAA"],
        cfg=RunnerSettings(
            enabled=True, bar_interval_minutes=5, bar_delay_seconds=3.0,
            cycle_deadline_seconds=6.0, retry_backoff_seconds=2.0, history_timeout_seconds=10.0,
            max_concurrent_requests=4, warmup_sessions=1, universe="fno_all",
            strategies={name: RunnerStrategy("python", "tp1_only")},
        ),
        mode="analyze",
        product="MIS",
        stale_limit_s=60.0,
        halted=lambda: False,
        price_band=(50.0, 1500.0),
        executor=ThreadPoolExecutor(2),
    )


class TestCycleWithTheTag:
    def test_pines_row_in_trades_db_does_not_block_the_python_signal(self, tmp_path):
        """Same symbol and bar as a Pine BREAKOUT row: different strategy, must still dispatch."""
        dispatch = AsyncMock()
        deps = _deps(tmp_path, PY, DispatchSink(dispatch), pine_sig_ids={"AAA-20260930-1000"})
        try:
            rep = asyncio.run(run_cycle(t("10:05"), deps))
            assert rep.submitted == 1
            sent = dispatch.await_args.args[0]
            assert (sent.strategy, sent.sig_id) == (PY, "BREAKOUT-PY-AAA-20260930-1000")
        finally:
            deps.state.close()

    def test_a_dispatching_sink_still_records_the_runners_own_events(self, tmp_path):
        """The shadow report reads the runner's event log; dispatch mode must feed it too."""
        deps = _deps(tmp_path, PY, DispatchSink(AsyncMock()))
        try:
            asyncio.run(run_cycle(t("10:05"), deps))
            events = deps.state.shadow_events(DAY, PY)
            assert [(e.kind, e.sig_id) for e in events] == [
                ("signal", "BREAKOUT-PY-AAA-20260930-1000")
            ]
        finally:
            deps.state.close()

    def test_a_python_signal_already_dispatched_is_not_sent_twice(self, tmp_path):
        dispatch = AsyncMock()
        deps = _deps(tmp_path, PY, DispatchSink(dispatch))
        try:
            asyncio.run(run_cycle(t("10:05"), deps))
            asyncio.run(run_cycle(t("10:05"), deps))
            assert dispatch.await_count == 1
        finally:
            deps.state.close()


# ---- paper-only safety -----------------------------------------------------------------------


def _phase(value):
    async def get():
        return value() if callable(value) else value

    return get


class TestPaperOnlySink:
    def test_analyze_mode_dispatches(self):
        dispatch = AsyncMock()
        sink = DispatchSink(dispatch, paper_only=True, phase=_phase("analyze"))
        asyncio.run(sink.submit(_sig()))
        dispatch.assert_awaited_once()

    def test_live_mode_is_refused_loudly_and_nothing_is_dispatched(self, logs):
        dispatch = AsyncMock()
        sink = DispatchSink(dispatch, paper_only=True, phase=_phase("live"))
        asyncio.run(sink.submit(_sig()))
        dispatch.assert_not_awaited()
        assert any("CRITICAL" in line and "paper-only" in line.lower() for line in logs)

    def test_a_mode_flip_mid_session_stops_dispatch(self):
        state = {"phase": "analyze"}
        dispatch = AsyncMock()
        sink = DispatchSink(dispatch, paper_only=True, phase=_phase(lambda: state["phase"]))
        asyncio.run(sink.submit(_sig(symbol="AAA")))
        state["phase"] = "live"
        asyncio.run(sink.submit(_sig(symbol="BBB")))
        assert [c.args[0].symbol for c in dispatch.await_args_list] == ["AAA"]

    def test_engine_started_in_live_mode_refuses_even_if_the_probe_says_analyze(self):
        mode_guard.set_startup_phase("live")
        dispatch = AsyncMock()
        sink = DispatchSink(dispatch, paper_only=True, phase=_phase("analyze"))
        asyncio.run(sink.submit(_sig()))
        dispatch.assert_not_awaited()

    def test_an_unreadable_mode_fails_closed(self):
        async def boom():
            raise OSError("openalgo unreachable")

        dispatch = AsyncMock()
        sink = DispatchSink(dispatch, paper_only=True, phase=boom)
        asyncio.run(sink.submit(_sig()))
        dispatch.assert_not_awaited()

    def test_exit_signals_are_held_to_the_same_rule(self):
        dispatch = AsyncMock()
        sink = DispatchSink(dispatch, paper_only=True, phase=_phase("live"))
        exit_sig = _sig()
        exit_sig.direction = Direction.EXIT
        asyncio.run(sink.submit(exit_sig))
        dispatch.assert_not_awaited()

    def test_without_the_flag_the_sink_does_not_consult_the_mode(self):
        dispatch = AsyncMock()
        sink = DispatchSink(dispatch, phase=_phase("live"))
        asyncio.run(sink.submit(_sig(strategy="ORB")))
        dispatch.assert_awaited_once()

    def test_build_sink_marks_a_paper_only_strategy(self):
        cfg = RunnerSettings(
            enabled=True, bar_interval_minutes=5, bar_delay_seconds=3.0,
            cycle_deadline_seconds=25.0, retry_backoff_seconds=2.0, history_timeout_seconds=10.0,
            max_concurrent_requests=8, warmup_sessions=20, universe=("TCS",),
            strategies={PY: RunnerStrategy("python", "tp1_only", "ladder", paper_only=True)},
        )
        sink = service.build_sink(cfg, PY, AsyncMock())
        assert sink.dispatches and sink.paper_only is True


class TestServiceStart:
    def _cfg(self, paper_only=True):
        return RunnerSettings(
            enabled=True, bar_interval_minutes=5, bar_delay_seconds=3.0,
            cycle_deadline_seconds=25.0, retry_backoff_seconds=2.0, history_timeout_seconds=10.0,
            max_concurrent_requests=8, warmup_sessions=20, universe=("TCS",),
            strategies={PY: RunnerStrategy("python", "tp1_only", "ladder", paper_only=paper_only)},
        )

    def test_live_start_up_launches_no_paper_only_strategy(self, logs, tmp_path):
        from signal_engine.tests.test_pipeline_service import FakeContext, FakeSource

        result = asyncio.run(
            service.start(
                self._cfg(), dispatch=AsyncMock(), tracker=None, trade_mode="live",
                time_exit=(14, 45), halted=lambda: False, stale_limit_s=60.0, product="MIS",
                source=FakeSource(), loader=object(),
                historify_symbols=lambda: ["TCS"], state=RunnerStateStore(str(tmp_path / "s.db")),
            )
        )
        assert result is None
        assert any("CRITICAL" in line and PY in line for line in logs)


# ---- config ----------------------------------------------------------------------------------


class TestConsistency:
    def _cfg(self, **entry):
        return RunnerSettings(
            enabled=True, bar_interval_minutes=5, bar_delay_seconds=3.0,
            cycle_deadline_seconds=25.0, retry_backoff_seconds=2.0, history_timeout_seconds=10.0,
            max_concurrent_requests=8, warmup_sessions=20, universe=("TCS",),
            strategies={PY: RunnerStrategy("python", "tp1_only", "ladder", **entry)},
        )

    def test_a_paper_only_python_strategy_is_coherent(self):
        single_source.check_consistency(self._cfg(paper_only=True), REGISTRY.keys())

    def test_a_python_strategy_that_could_go_live_is_refused_at_start_up(self):
        with pytest.raises(single_source.SingleSourceError, match="paper_only"):
            single_source.check_consistency(self._cfg(paper_only=False), REGISTRY.keys())


class TestShippedConfig:
    runner = settings.runner

    def test_python_tag_is_python_sourced_ladder_managed_and_paper_only(self):
        entry = self.runner.strategies[PY]
        assert (entry.signal_source, entry.exit_plan) == ("python", "tp1_only")
        assert (entry.exit_owner, entry.paper_only) == ("ladder", True)

    def test_pine_tag_is_never_python_sourced_or_ladder_managed(self):
        entry = self.runner.strategies.get("BREAKOUT")
        assert entry is None or (entry.signal_source, entry.exit_owner) == ("pine", "tracker")
        assert not single_source.should_drop_alert(self.runner, "BREAKOUT")

    def test_pine_alerts_for_the_python_tag_would_be_dropped(self):
        assert single_source.should_drop_alert(self.runner, PY)

    def test_shipped_config_is_coherent(self):
        single_source.check_consistency(self.runner, REGISTRY.keys())

    def test_python_tag_has_a_bhel_block_and_a_tight_stop_floor_like_pine(self):
        assert "BHEL" in settings.blacklist[PY]
        profile = settings.strategy_profiles[PY]
        assert profile["product"] == "MIS"
        assert profile["min_sl_pct"] == settings.strategy_profiles["BREAKOUT"]["min_sl_pct"]

    def test_only_the_python_tag_is_ladder_owned(self):
        from signal_engine.tracker import _ladder_owns_exits

        assert _ladder_owns_exits(settings, PY) is True
        assert _ladder_owns_exits(settings, "BREAKOUT") is False  # Pine exits stay with the tracker

    def test_both_tags_trade_the_same_price_band(self):
        from signal_engine.pipeline import pretrade

        assert pretrade.price_band(PY) == pretrade.price_band("BREAKOUT")

    def test_analyze_mode_needs_no_cap_change_for_a_fourth_sandbox_strategy(self):
        analyze = settings.mode_profiles["analyze"]
        assert (analyze["max_open_positions"], analyze["max_trades_per_day"]) == (0, 0)


# ---- isolation from the Pine strategy --------------------------------------------------------


class TestRiskIsolation:
    def _engine(self):
        from signal_engine.tests.risk_fixtures import _engine

        return _engine(use_day_start_capital=True, max_positions_per_symbol=1)

    def test_each_tag_sizes_off_its_own_capital_pool(self):
        engine = self._engine()
        assert engine.get_sizing_capital(100_000, "BREAKOUT") == 100_000
        assert engine.get_sizing_capital(40_000, PY) == 40_000
        assert engine.get_sizing_capital(10_000, "BREAKOUT") == 100_000

    def test_the_same_symbol_can_be_held_by_both_tags(self):
        engine = self._engine()
        engine.record_trade("BREAKOUT", "TCS")
        assert engine.can_trade_symbol("TCS", PY) is True
        engine.record_trade(PY, "TCS")
        assert engine.can_trade_symbol("TCS", PY) is False  # no averaging within one tag
        assert engine.can_trade_symbol("TCS", "BREAKOUT") is False

    def test_counters_do_not_leak_between_tags(self):
        engine = self._engine()
        engine.record_trade("BREAKOUT", "TCS")
        engine.record_trade(PY, "INFY")
        engine.record_close(-500.0, PY, "INFY")
        assert engine.open_positions_for("BREAKOUT") == 1
        assert engine.trades_today_for("BREAKOUT") == 1
        assert engine.open_positions_for(PY) == 0
        assert engine.trades_today_for(PY) == 1


# ---- shadow report ---------------------------------------------------------------------------


class TestShadowReportWithThePythonTag:
    def test_prefixed_runner_signals_line_up_with_plain_pine_rows(self):
        from signal_engine.pipeline.shadow_report import PineSignal, compare
        from signal_engine.pipeline.state import ShadowEvent

        bar = pd.Timestamp("2026-09-30 10:00", tz=IST)
        ours = ShadowEvent(DAY, PY, "TCS", bar, "signal", "LONG", f"{PY}-TCS-20260930-1000")
        pine = PineSignal("TCS-20260930-1000", "TCS", "LONG", bar, "SUCCESS", "")
        rep = compare([ours], [pine], universe={"TCS"}, last_bars={}, runner_strategy=PY)
        assert len(rep.matched) == 1
        assert rep.runner_only == [] and rep.pine_only == []

    def test_without_the_prefix_handling_they_would_not_match(self):
        from signal_engine.pipeline.shadow_report import PineSignal, compare
        from signal_engine.pipeline.state import ShadowEvent

        bar = pd.Timestamp("2026-09-30 10:00", tz=IST)
        ours = ShadowEvent(DAY, PY, "TCS", bar, "signal", "LONG", f"{PY}-TCS-20260930-1000")
        pine = PineSignal("TCS-20260930-1000", "TCS", "LONG", bar, "SUCCESS", "")
        rep = compare([ours], [pine], universe={"TCS"}, last_bars={})
        assert len(rep.matched) == 0 and len(rep.runner_only) == 1
