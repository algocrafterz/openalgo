"""The per-strategy paper limits must be live after the real start-up sequence (BREAKOUT spec v1.0 item 16).

main.py builds the risk engine at import time, in "live" mode, before the trading mode is known.
startup._run_engine then calls runtime.apply_trade_mode. These tests drive that sequence with the real
config.yaml. The earlier unit tests built the engine directly in analyze mode and so never saw that
apply_trade_mode left the overrides empty.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from signal_engine import runtime, startup
from signal_engine.risk_store import RiskStore
from signal_engine.runtime import build_risk_engine
from signal_engine.tests.test_startup_runner import _engine_patches

PY = "BREAKOUT-PY"
OTHER = "ORB"


def engine_after_startup(tmp_path, mode="analyze"):
    """Build the engine as main.py does, then run the real _run_engine with the real apply_trade_mode."""
    engine = build_risk_engine(RiskStore(str(tmp_path / "risk.db")))  # main.py: default trade_mode "live"
    with ExitStack() as stack:
        tracker, _ = _engine_patches(stack)
        stack.enter_context(
            patch.object(startup, "fetch_trading_mode", AsyncMock(return_value=(mode, mode == "analyze")))
        )
        stack.enter_context(patch.object(startup, "apply_trade_mode", runtime.apply_trade_mode))
        asyncio.run(startup._run_engine(engine, tracker, AsyncMock(), AsyncMock()))
    return engine


class TestStartupLoadsTheStrategyLimits:
    def test_the_engine_is_built_without_overrides_so_startup_has_to_load_them(self, tmp_path):
        engine = build_risk_engine(RiskStore(str(tmp_path / "risk.db")))
        assert engine._strategy_limits == {}

    def test_breakout_py_starts_with_ten_trades_and_the_live_loss_limits(self, tmp_path):
        engine = engine_after_startup(tmp_path)
        limits = engine._strategy_limits[PY]
        assert limits["max_trades_per_day"] == 10
        assert limits["daily_loss_limit"] == 0.04
        assert limits["weekly_loss_limit"] == 0.08
        assert limits["monthly_loss_limit"] == 0.15

    def test_only_breakout_py_has_an_override(self, tmp_path):
        assert set(engine_after_startup(tmp_path)._strategy_limits) == {PY}

    def test_the_cap_binds_breakout_py_and_no_other_strategy(self, tmp_path):
        engine = engine_after_startup(tmp_path)
        for tag in (PY, OTHER):
            for i in range(10):
                engine.record_trade(strategy=tag, symbol=f"S{i}")
                engine.record_close(pnl=1.0, strategy=tag, symbol=f"S{i}")
        assert engine.check_exposure(PY) is False
        assert "Max trades/day (10/10)" in engine.exposure_block_reason(PY)
        assert engine.check_exposure(OTHER) is True  # the analyze profile is unlimited

    def test_the_mode_wide_limits_are_unchanged(self, tmp_path):
        engine = engine_after_startup(tmp_path)
        assert engine.max_trades_per_day == 0
        assert engine.daily_loss_limit == 1.0

    def test_live_mode_carries_no_overrides(self, tmp_path):
        assert engine_after_startup(tmp_path, mode="live")._strategy_limits == {}

    def test_applying_the_mode_again_does_not_stack_or_leak_the_limits(self, tmp_path):
        engine = engine_after_startup(tmp_path)
        runtime.apply_trade_mode(engine, "analyze")
        assert engine._strategy_limits[PY]["max_trades_per_day"] == 10
        runtime.apply_trade_mode(engine, "live")
        assert engine._strategy_limits == {}
