"""ExitDriver: steps the shared Ladder per runner-managed position and emits EXIT signals."""

import datetime as dt

import pandas as pd
import pytest

from signal_engine.models import Direction
from signal_engine.pipeline.exit_driver import ExitDriver, plan_for
from signal_engine.strategy_core.exits import ExitPlan
from signal_engine.timeutils import IST
from signal_engine.tracker import TrackedPosition

DAY = dt.date(2026, 9, 30)


def ts(h, m):
    return pd.Timestamp(dt.datetime(2026, 9, 30, h, m), tz=IST)


class FakePositions:
    def __init__(self, *positions):
        self.by_key = {(p.symbol, p.strategy): p for p in positions}

    def find_position(self, symbol, strategy):
        return self.by_key.get((symbol, strategy))

    def remove(self, symbol, strategy):
        self.by_key.pop((symbol, strategy), None)


class FakeBroker:
    def __init__(self, qty=0):
        self.qty = qty
        self.calls = 0

    async def __call__(self, pos):
        self.calls += 1
        return self.qty


def make_pos(direction=Direction.LONG, fill=100.0, sl=98.0, tp=104.0, ctx=None, **kw):
    base = {
        "symbol": "TCS",
        "strategy": "BREAKOUT",
        "exchange": "NSE",
        "product": "MIS",
        "entry_price": fill,
        "quantity": 100,
        "sl": sl,
        "tp": tp,
        "direction": direction,
        "fill_price": fill,
        "original_quantity": 100,
        "entry_time": dt.datetime(2026, 9, 30, 9, 55, 5, tzinfo=IST),
        "context": ctx if ctx is not None else {},
        "sig_id": "TCS-20260930-0955",
    }
    base.update(kw)
    return TrackedPosition(**base)


def driver(pos, plan="tp1_only", broker=None, safety_bars=2, positions=None):
    return ExitDriver(
        strategy="BREAKOUT",
        plan=plan_for(plan),
        positions=positions or FakePositions(pos),
        broker_qty=broker or FakeBroker(0),
        interval_min=5,
        safety_bars=safety_bars,
    )


class TestPlanFor:
    def test_tp1_only_is_a_single_full_leg(self):
        p = plan_for("tp1_only")
        assert p.fractions == (1.0,) and p.stop_rule == "none"

    def test_ladder_plan_matches_the_config_name(self):
        p = plan_for("ladder_30_35_35")
        assert p.fractions == (0.30, 0.35, 0.35) and p.stop_rule == "tp_buffer"
        assert isinstance(p, ExitPlan)

    def test_unknown_plan_raises(self):
        with pytest.raises(ValueError, match="nope"):
            plan_for("nope")


class TestTargets:
    @pytest.mark.asyncio
    async def test_tp1_only_full_exit_signal(self):
        d = driver(make_pos())
        sigs = await d.on_bar("TCS", ts(10, 0), 100.5, 104.5, 100.0)
        assert len(sigs) == 1
        s = sigs[0]
        assert s.direction == Direction.EXIT and s.symbol == "TCS" and s.strategy == "BREAKOUT"
        assert s.tp_level == "TP1" and s.exit_qty_pct == pytest.approx(1.0)
        assert s.context["ladder_sl"] == pytest.approx(98.0)
        assert s.exchange == "NSE" and s.product == "MIS"

    @pytest.mark.asyncio
    async def test_nothing_happens_inside_the_range(self):
        d = driver(make_pos())
        assert await d.on_bar("TCS", ts(10, 0), 100.5, 103.0, 99.0) == []

    @pytest.mark.asyncio
    async def test_short_target(self):
        pos = make_pos(direction=Direction.SHORT, sl=102.0, tp=96.0)
        sigs = await driver(pos).on_bar("TCS", ts(10, 0), 99.5, 100.0, 95.5)
        assert [s.tp_level for s in sigs] == ["TP1"]

    @pytest.mark.asyncio
    async def test_ladder_fractions_are_relative_to_the_remaining_position(self):
        pos = make_pos(ctx={"tp2": 108.0, "tp3": 112.0})
        d = driver(pos, "ladder_30_35_35")
        s1 = await d.on_bar("TCS", ts(10, 0), 100.5, 104.5, 100.0)
        assert [(s.tp_level, s.exit_qty_pct) for s in s1] == [("TP1", pytest.approx(0.30))]
        # 30 percent sold; TP1 hit moves the stop to 104 - 0.3 * 4 = 102.8
        assert s1[0].context["ladder_sl"] == pytest.approx(102.8)
        s2 = await d.on_bar("TCS", ts(10, 5), 104.0, 108.5, 103.5)
        assert [(s.tp_level, s.exit_qty_pct) for s in s2] == [("TP2", pytest.approx(0.35 / 0.70))]
        s3 = await d.on_bar("TCS", ts(10, 10), 108.0, 112.5, 107.0)
        assert [(s.tp_level, s.exit_qty_pct) for s in s3] == [("TP3", pytest.approx(1.0))]

    @pytest.mark.asyncio
    async def test_one_bar_through_two_targets_emits_both_in_order(self):
        pos = make_pos(ctx={"tp2": 108.0, "tp3": 112.0})
        d = driver(pos, "ladder_30_35_35")
        sigs = await d.on_bar("TCS", ts(10, 0), 101.0, 109.0, 100.5)
        assert [s.tp_level for s in sigs] == ["TP1", "TP2"]
        assert sigs[0].exit_qty_pct == pytest.approx(0.30)
        assert sigs[1].exit_qty_pct == pytest.approx(0.35 / 0.70)

    @pytest.mark.asyncio
    async def test_ladder_plan_without_enough_targets_is_skipped_once_and_logged(self, caplog):
        d = driver(make_pos(ctx={}), "ladder_30_35_35")
        assert await d.on_bar("TCS", ts(10, 0), 100.5, 105.0, 100.0) == []
        assert await d.on_bar("TCS", ts(10, 5), 100.5, 105.0, 100.0) == []
        assert d.unmanaged == {"TCS"}


class TestLazyLadder:
    @pytest.mark.asyncio
    async def test_anchored_to_the_actual_fill_not_the_signal_price(self):
        """Fill 100.4 with stop 98: a bar low of 98.2 must not stop out, 97.9 must."""
        pos = make_pos(fill=100.4, sl=98.0, tp=104.0)
        d = driver(pos)
        assert await d.on_bar("TCS", ts(10, 0), 100.4, 101.0, 98.2) == []
        assert d.ladder("TCS").entry == pytest.approx(100.4)

    @pytest.mark.asyncio
    async def test_falls_back_to_entry_price_when_fill_is_unknown(self):
        pos = make_pos(fill=0.0, entry_price=100.0)
        d = driver(pos)
        await d.on_bar("TCS", ts(10, 0), 100.0, 101.0, 99.5)
        assert d.ladder("TCS").entry == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_bars_that_closed_before_the_entry_are_not_stepped(self):
        """Entry at 09:55:05, so the 09:50 bar (closes 09:55) must not touch the new ladder."""
        d = driver(make_pos())
        assert await d.on_bar("TCS", ts(9, 50), 100.0, 110.0, 90.0) == []
        assert d.ladder("TCS") is None
        sigs = await d.on_bar("TCS", ts(9, 55), 100.0, 105.0, 99.5)
        assert [s.tp_level for s in sigs] == ["TP1"]

    @pytest.mark.asyncio
    async def test_no_position_no_state(self):
        d = driver(None, positions=FakePositions())
        assert await d.on_bar("TCS", ts(10, 0), 100.0, 110.0, 90.0) == []
        assert d.ladder("TCS") is None

    @pytest.mark.asyncio
    async def test_state_is_dropped_when_the_position_disappears(self):
        pos = make_pos()
        book = FakePositions(pos)
        d = driver(pos, positions=book)
        await d.on_bar("TCS", ts(10, 0), 100.0, 101.0, 99.5)
        assert d.ladder("TCS") is not None
        book.remove("TCS", "BREAKOUT")
        await d.on_bar("TCS", ts(10, 5), 100.0, 101.0, 99.5)
        assert d.ladder("TCS") is None


class TestStopHit:
    @pytest.mark.asyncio
    async def test_stop_hit_sends_no_order_while_the_broker_stop_may_still_fire(self):
        d = driver(make_pos())
        assert await d.on_bar("TCS", ts(10, 0), 99.0, 100.0, 97.0) == []

    @pytest.mark.asyncio
    async def test_no_safety_exit_when_the_tracker_closes_the_position(self):
        pos = make_pos()
        book = FakePositions(pos)
        broker = FakeBroker(0)
        d = driver(pos, broker=broker, positions=book)
        await d.on_bar("TCS", ts(10, 0), 99.0, 100.0, 97.0)
        book.remove("TCS", "BREAKOUT")
        assert await d.on_bar("TCS", ts(10, 5), 97.0, 98.0, 96.0) == []
        assert await d.on_bar("TCS", ts(10, 10), 97.0, 98.0, 96.0) == []
        assert broker.calls == 0

    @pytest.mark.asyncio
    async def test_safety_exit_reconciles_when_the_broker_is_already_flat(self):
        broker = FakeBroker(0)
        d = driver(make_pos(), broker=broker, safety_bars=2)
        await d.on_bar("TCS", ts(10, 0), 99.0, 100.0, 97.0)
        assert await d.on_bar("TCS", ts(10, 5), 97.0, 98.0, 96.0) == []
        sigs = await d.on_bar("TCS", ts(10, 10), 97.0, 98.0, 96.0)
        assert [s.tp_level for s in sigs] == ["SL"]

    @pytest.mark.asyncio
    async def test_safety_exit_is_a_real_full_exit_when_the_broker_still_holds(self):
        """Reconciling would cancel the live stop and forget an open position."""
        broker = FakeBroker(100)
        d = driver(make_pos(), broker=broker, safety_bars=1)
        await d.on_bar("TCS", ts(10, 0), 99.0, 100.0, 97.0)
        sigs = await d.on_bar("TCS", ts(10, 5), 97.0, 98.0, 96.0)
        assert len(sigs) == 1
        assert sigs[0].tp_level is None and sigs[0].exit_qty_pct == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_safety_exit_is_sent_once(self):
        d = driver(make_pos(), broker=FakeBroker(0), safety_bars=1)
        await d.on_bar("TCS", ts(10, 0), 99.0, 100.0, 97.0)
        assert len(await d.on_bar("TCS", ts(10, 5), 97.0, 98.0, 96.0)) == 1
        assert await d.on_bar("TCS", ts(10, 10), 97.0, 98.0, 96.0) == []

    @pytest.mark.asyncio
    async def test_broker_api_error_defers_and_retries(self):
        broker = FakeBroker(-1)
        d = driver(make_pos(), broker=broker, safety_bars=1)
        await d.on_bar("TCS", ts(10, 0), 99.0, 100.0, 97.0)
        assert await d.on_bar("TCS", ts(10, 5), 97.0, 98.0, 96.0) == []
        broker.qty = 0
        assert len(await d.on_bar("TCS", ts(10, 10), 97.0, 98.0, 96.0)) == 1


class TestTimeExit:
    @pytest.mark.asyncio
    async def test_time_exit_closes_the_ladder_once(self):
        d = driver(make_pos())
        await d.on_bar("TCS", ts(10, 0), 100.0, 101.0, 99.5)
        d.on_time_exit("TCS", 100.7)
        d.on_time_exit("TCS", 100.9)
        lad = d.ladder("TCS")
        assert lad.done and [f.reason for f in lad.fills] == ["TIME"]
        assert lad.fills[0].price == pytest.approx(100.7)

    @pytest.mark.asyncio
    async def test_no_signals_after_a_time_exit(self):
        d = driver(make_pos())
        await d.on_bar("TCS", ts(10, 0), 100.0, 101.0, 99.5)
        d.on_time_exit("TCS", 100.7)
        assert await d.on_bar("TCS", ts(10, 5), 100.0, 110.0, 90.0) == []

    def test_time_exit_without_a_ladder_is_a_no_op(self):
        driver(make_pos()).on_time_exit("TCS", 100.0)
