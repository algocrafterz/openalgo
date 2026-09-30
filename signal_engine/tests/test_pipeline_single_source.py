"""Exactly one signal source per strategy; a mismatch drops alerts or refuses to start."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.config import RunnerSettings, RunnerStrategy
from signal_engine.pipeline import single_source as ss
from signal_engine.pipeline.single_source import SingleSourceError


def cfg(source="pine", enabled=True, tag="BREAKOUT") -> RunnerSettings:
    return RunnerSettings(
        enabled=enabled,
        bar_interval_minutes=5,
        bar_delay_seconds=3.0,
        cycle_deadline_seconds=25.0,
        retry_backoff_seconds=2.0,
        history_timeout_seconds=10.0,
        max_concurrent_requests=8,
        warmup_sessions=20,
        universe="fno_all",
        strategies={tag: RunnerStrategy(source, "tp1_only")},
    )


@pytest.fixture(autouse=True)
def _reset():
    ss.reset_counts()


class TestShouldDrop:
    def test_python_sourced_strategy_drops_pine_alerts(self):
        assert ss.should_drop_alert(cfg("python"), "BREAKOUT")

    def test_pine_sourced_strategy_keeps_alerts(self):
        assert not ss.should_drop_alert(cfg("pine"), "BREAKOUT")

    def test_unlisted_strategy_keeps_alerts(self):
        assert not ss.should_drop_alert(cfg("python"), "ORB")

    def test_case_insensitive(self):
        assert ss.should_drop_alert(cfg("python"), "breakout")

    def test_drop_is_counted(self):
        for _ in range(3):
            ss.record_drop("BREAKOUT")
        assert ss.dropped_count("BREAKOUT") == 3


class TestAssertCanDispatch:
    def test_python_source_may_dispatch(self):
        ss.assert_can_dispatch(cfg("python"), "BREAKOUT", sink_dispatches=True)

    def test_pine_source_must_not_dispatch(self):
        with pytest.raises(SingleSourceError, match="BREAKOUT"):
            ss.assert_can_dispatch(cfg("pine"), "BREAKOUT", sink_dispatches=True)

    def test_shadow_sink_never_dispatches_so_pine_is_fine(self):
        ss.assert_can_dispatch(cfg("pine"), "BREAKOUT", sink_dispatches=False)

    def test_unlisted_strategy_must_not_dispatch(self):
        with pytest.raises(SingleSourceError):
            ss.assert_can_dispatch(cfg("python"), "ORB", sink_dispatches=True)


class TestConsistency:
    def test_pine_and_disabled_is_consistent(self):
        ss.check_consistency(cfg("pine", enabled=False), {"BREAKOUT"})

    def test_python_source_with_runner_disabled_refuses_to_start(self):
        with pytest.raises(SingleSourceError, match="disabled"):
            ss.check_consistency(cfg("python", enabled=False), {"BREAKOUT"})

    def test_ladder_owner_with_pine_source_refuses_to_start(self):
        c = cfg("pine")
        c = RunnerSettings(
            **{**c.__dict__, "strategies": {"BREAKOUT": RunnerStrategy("pine", "tp1_only", "ladder")}}
        )
        with pytest.raises(ss.SingleSourceError, match="exit_owner ladder"):
            ss.check_consistency(c, ["BREAKOUT"])

    def test_unregistered_strategy_refuses_to_start(self):
        with pytest.raises(SingleSourceError, match="NOPE"):
            ss.check_consistency(cfg("pine", tag="NOPE"), {"BREAKOUT"})

    def test_all_problems_are_reported_together(self):
        with pytest.raises(SingleSourceError) as e:
            ss.check_consistency(cfg("python", enabled=False, tag="NOPE"), {"BREAKOUT"})
        assert "NOPE" in str(e.value) and "disabled" in str(e.value)

    def test_describe_lists_the_active_source_per_strategy(self):
        lines = ss.describe(cfg("python"))
        assert any("BREAKOUT" in ln and "python" in ln for ln in lines)


class TestHandleMessageDropsPineAlertsForPythonStrategies:
    MSG = "BREAKOUT LONG\nSymbol: TCS\nEntry: 100\nSL: 99\nTP: 101.5"

    def test_entry_and_exit_alerts_are_dropped_and_counted(self):
        from signal_engine import main

        exit_msg = "BREAKOUT EXIT\nSymbol: TCS\nTP HIT: TP1"
        with (
            patch.object(main, "settings", SimpleNamespace(runner=cfg("python"))),
            patch.object(main, "_handle_entry", new_callable=AsyncMock) as entry,
            patch.object(main, "_handle_exit", new_callable=AsyncMock) as ex,
            patch.object(main, "validate") as val,
        ):
            import asyncio

            asyncio.run(main.handle_message(self.MSG))
            asyncio.run(main.handle_message(exit_msg))
        entry.assert_not_called()
        ex.assert_not_called()
        val.assert_not_called()
        assert ss.dropped_count("BREAKOUT") >= 1

    def test_pine_source_still_flows_to_validation(self):
        from signal_engine import main
        from signal_engine.models import ValidationResult, ValidationStatus

        with (
            patch.object(main, "settings", SimpleNamespace(runner=cfg("pine"))),
            patch.object(main, "_handle_entry", new_callable=AsyncMock) as entry,
            patch.object(
                main, "validate", return_value=ValidationResult(status=ValidationStatus.VALID)
            ),
        ):
            import asyncio

            asyncio.run(main.handle_message(self.MSG))
        entry.assert_awaited_once()
