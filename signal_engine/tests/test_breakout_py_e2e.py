"""BREAKOUT-PY through the real entry and exit path in analyze mode, with a fake broker.

A runner Signal tagged BREAKOUT-PY reaches _handle_entry, is sized off its own capital pool,
registers its own tracker position next to a Pine BREAKOUT position in the same symbol, and its
Ladder exits reach the broker with the Ladder's own stop (override_sl), not the old hand rule.
"""

import datetime as dt
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from signal_engine.main import _handle_entry, _handle_exit, dispatch_signal
from signal_engine.models import Direction, Signal, ValidationResult, ValidationStatus
from signal_engine.pipeline.exit_driver import ExitDriver, plan_for
from signal_engine.tests.test_main_characterization import (
    _ok,
    _position,
    _real_tracker,
    _settings_stub,
    harness,
)
from signal_engine.timeutils import IST

PY = "BREAKOUT-PY"
ANALYZE = AsyncMock(return_value=("analyze", True))


def _profiles():
    return {"BREAKOUT": {"product": "MIS"}, PY: {"product": "MIS"}}


def _entry(strategy=PY, symbol="RELIANCE"):
    return Signal(
        strategy=strategy,
        direction=Direction.LONG,
        symbol=symbol,
        entry=2500.0,
        sl=2485.0,
        tp=2530.0,
        exchange="NSE",
        product="MIS",
        time="09:55",
        sig_id=f"{PY}-{symbol}-20260930-0955",
        context={"trigger": "PDH-RT", "tp2": "2545.0", "tp3": "2560.0"},
    )


class TestEntry:
    @pytest.mark.asyncio
    async def test_dispatch_reaches_handle_entry_with_the_python_tag(self):
        valid = ValidationResult(status=ValidationStatus.VALID)
        with (
            patch("signal_engine.main.validate", return_value=valid),
            patch("signal_engine.main._handle_entry", new_callable=AsyncMock) as entry,
        ):
            sig = _entry()
            await dispatch_signal(sig)
        entry.assert_awaited_once_with(sig)
        assert entry.await_args.args[0].strategy == PY

    @pytest.mark.asyncio
    async def test_entry_is_sized_gated_and_tracked_under_its_own_tag(self):
        tracker = _real_tracker()
        h = harness(
            tracker=tracker,
            settings=_settings_stub(strategy_profiles=_profiles()),
            fetch_trading_mode=ANALYZE,
        )
        with h.stack:
            await _handle_entry(_entry())
        h.send_order.assert_awaited_once()
        assert h.risk.get_sizing_capital.call_args.args[-1] == PY
        assert h.risk.check_exposure.call_args.args[-1] == PY
        pos = tracker.find_position("RELIANCE", PY)
        assert pos is not None and pos.strategy == PY
        assert pos.sig_id == f"{PY}-RELIANCE-20260930-0955"

    @pytest.mark.asyncio
    async def test_a_pine_position_in_the_same_symbol_is_left_alone(self):
        tracker = _real_tracker()
        tracker.register(_position(strategy="BREAKOUT", quantity=40))
        h = harness(
            tracker=tracker,
            settings=_settings_stub(strategy_profiles=_profiles()),
            fetch_trading_mode=ANALYZE,
        )
        with h.stack:
            await _handle_entry(_entry())
        pine = tracker.find_position("RELIANCE", "BREAKOUT")
        py = tracker.find_position("RELIANCE", PY)
        assert pine is not None and pine.quantity == 40
        assert py is not None and py is not pine


def _ladder_position(tag=PY):
    return _position(
        strategy=tag,
        quantity=100,
        original_quantity=100,
        entry_price=2500.0,
        fill_price=2500.0,
        sl=2490.0,
        tp=2510.0,
        sig_id=f"{tag}-RELIANCE-20260930-0955",
        entry_time=dt.datetime(2026, 9, 30, 9, 55, 5, tzinfo=IST),
        context={"tp2": "2520.0", "tp3": "2530.0"},
    )


class TestExits:
    @pytest.mark.asyncio
    async def test_ladder_target_exits_the_tagged_position_and_places_the_ladder_stop(self):
        tracker = _real_tracker()
        tracker.register(_ladder_position())
        driver = ExitDriver(
            strategy=PY,
            plan=plan_for("ladder_30_35_35"),
            positions=tracker,
            broker_qty=AsyncMock(return_value=100),
            interval_min=5,
            safety_bars=2,
        )
        bar = pd.Timestamp("2026-09-30 10:00", tz=IST)
        signals = await driver.on_bar("RELIANCE", bar, 2501.0, 2511.0, 2500.0, 2505.5)
        assert [s.tp_level for s in signals] == ["TP1"]
        assert signals[0].strategy == PY
        ladder_sl = signals[0].context["ladder_sl"]
        assert ladder_sl == driver.ladder("RELIANCE").sl_eff

        h = harness(
            tracker=tracker,
            settings=_settings_stub(strategy_profiles=_profiles()),
            fetch_realised_pnl=AsyncMock(return_value=300.0),
            place_sl_order=AsyncMock(return_value=_ok("SL9")),
        )
        with h.stack:
            await _handle_exit(signals[0])
        assert h.place_sl_order.call_args.kwargs["sl_price"] == pytest.approx(ladder_sl)
        pos = tracker.find_position("RELIANCE", PY)
        assert pos is not None and pos.quantity == 70  # 30% booked at TP1

    @pytest.mark.asyncio
    async def test_a_python_exit_never_touches_the_pine_position(self):
        tracker = _real_tracker()
        tracker.register(_ladder_position())
        tracker.register(_ladder_position(tag="BREAKOUT"))
        driver = ExitDriver(
            strategy=PY,
            plan=plan_for("tp1_only"),
            positions=tracker,
            broker_qty=AsyncMock(return_value=100),
            interval_min=5,
            safety_bars=2,
        )
        bar = pd.Timestamp("2026-09-30 10:00", tz=IST)
        (exit_signal,) = await driver.on_bar("RELIANCE", bar, 2501.0, 2511.0, 2500.0, 2505.5)
        h = harness(
            tracker=tracker,
            settings=_settings_stub(strategy_profiles=_profiles()),
            fetch_realised_pnl=AsyncMock(return_value=300.0),
        )
        with h.stack:
            await _handle_exit(exit_signal)
        assert tracker.find_position("RELIANCE", PY) is None
        pine = tracker.find_position("RELIANCE", "BREAKOUT")
        assert pine is not None and pine.quantity == 100
