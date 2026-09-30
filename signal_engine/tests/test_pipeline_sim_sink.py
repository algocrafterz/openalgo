"""SimSink: a simulated broker plus tracker with the backtest engine's fill semantics."""

import asyncio

import pandas as pd
import pytest

from signal_engine.backtest.types import RunConfig
from signal_engine.models import Direction, Signal
from signal_engine.pipeline.sim_sink import SimRules, SimSink
from signal_engine.timeutils import IST


def t(hm):
    return pd.Timestamp(f"2026-09-30 {hm}", tz=IST)


def rules(**kw):
    base = {
        "max_trades_per_day": 2,
        "one_trade_per_direction": True,
        "allow_longs": True,
        "allow_shorts": True,
        "time_exit_min": 14 * 60 + 45,
        "cost_bps": 0.0,
    }
    base.update(kw)
    return SimRules(**base)


def entry(direction=Direction.LONG, price=100.0, sl=98.0, tp=104.0, symbol="TCS"):
    return Signal(
        strategy="BREAKOUT",
        direction=direction,
        symbol=symbol,
        entry=price,
        sl=sl,
        tp=tp,
        exchange="NSE",
        product="MIS",
        sig_id=f"{symbol}-20260930-0955",
        context={"tp2": tp + 4, "tp3": tp + 8, "trigger": "PDH-RT"},
    )


def exit_sig(tp_level, pct, fill_price=None, ladder_sl=None, symbol="TCS"):
    ctx = {}
    if fill_price is not None:
        ctx["ladder_fill_price"] = fill_price
    if ladder_sl is not None:
        ctx["ladder_sl"] = ladder_sl
    return Signal(
        strategy="BREAKOUT",
        direction=Direction.EXIT,
        symbol=symbol,
        entry=0.0,
        sl=ladder_sl or 0.0,
        tp=104.0,
        tp_level=tp_level,
        exit_qty_pct=pct,
        context=ctx,
    )


def go(coro):
    return asyncio.run(coro)


class Env:
    def __init__(self, **kw):
        self.sink = SimSink(rules(**kw), interval_min=5)

    def submit(self, sig):
        go(self.sink.submit(sig))

    def bar(self, hm, o, h, low, c):
        go(self.sink.on_bar("TCS", t(hm), o, h, low, c))

    def after(self, hm, o, h, low, c):
        go(self.sink.after_bar("TCS", t(hm), o, h, low, c))

    @property
    def pos(self):
        return self.sink.find_position("TCS", "BREAKOUT")


class TestRules:
    def test_from_run_config_caps_time_exit_before_the_broker_squareoff(self):
        r = SimRules.from_run_config(RunConfig(time_exit_min=15 * 60), bar_min=5)
        assert r.time_exit_min == 15 * 60 - 5 - 5
        r2 = SimRules.from_run_config(RunConfig(), bar_min=5)
        assert r2.time_exit_min == 14 * 60 + 45
        assert r2.cost_bps == RunConfig().cost_bps


class TestEntryFill:
    def test_entry_is_pending_until_the_next_bar_then_fills_at_its_open(self):
        e = Env()
        e.submit(entry())
        assert e.pos is None
        e.bar("10:00", 100.5, 101.0, 100.0, 100.8)
        p = e.pos
        assert p.fill_price == 100.5 and p.sl == 98.0 and p.tp == 104.0
        assert p.direction == Direction.LONG and p.entry_time == t("10:00").to_pydatetime()
        assert p.context["tp2"] == 108.0 and p.sig_id == "TCS-20260930-0955"

    def test_filling_through_the_stop_closes_flat_at_the_fill(self):
        e = Env()
        e.submit(entry())
        e.bar("10:00", 97.5, 98.5, 97.0, 98.0)
        assert e.pos is None
        (tr,) = e.sink.trades
        assert tr.reason == "SL_GAP_ENTRY" and tr.exit == 97.5 and tr.entry == 97.5


class TestBrokerStop:
    def test_stop_fills_at_the_stop_price(self):
        e = Env()
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 100.0, 100.5)
        e.bar("10:05", 100.5, 101.0, 97.9, 98.5)
        (tr,) = e.sink.trades
        assert (tr.reason, tr.exit) == ("SL", 98.0)
        assert tr.r_gross == pytest.approx((98.0 - 100.0) / 2.0)

    def test_gap_through_the_stop_fills_at_the_open(self):
        e = Env()
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 100.0, 100.5)
        e.bar("10:05", 97.0, 99.0, 96.5, 98.0)
        (tr,) = e.sink.trades
        assert (tr.reason, tr.exit) == ("SL_GAP", 97.0)

    def test_the_fill_bar_itself_can_stop_the_position_out(self):
        e = Env()
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 97.5, 98.5)
        (tr,) = e.sink.trades
        assert tr.reason == "SL"

    def test_short_stop(self):
        e = Env()
        e.submit(entry(Direction.SHORT, sl=102.0, tp=96.0))
        e.bar("10:00", 100.0, 100.5, 99.5, 100.0)
        e.bar("10:05", 100.0, 102.5, 99.5, 101.0)
        (tr,) = e.sink.trades
        assert (tr.reason, tr.exit) == ("SL", 102.0)
        assert tr.r_gross == pytest.approx(-1.0)


class TestExitSignals:
    def _open(self, e):
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 99.5, 100.5)

    def test_full_target_exit_books_a_tp_trade(self):
        e = Env()
        self._open(e)
        e.submit(exit_sig("TP1", 1.0, fill_price=104.0, ladder_sl=98.0))
        (tr,) = e.sink.trades
        assert (tr.reason, tr.exit, tr.legs) == ("TP", 104.0, 1)
        assert tr.r_gross == pytest.approx(2.0)
        assert e.pos is None

    def test_partial_then_final_is_size_weighted_with_a_tightened_stop(self):
        e = Env()
        self._open(e)
        e.submit(exit_sig("TP1", 0.3, fill_price=104.0, ladder_sl=102.8))
        assert e.pos is not None and e.pos.sl == pytest.approx(102.8)
        e.submit(exit_sig("TP2", 1.0, fill_price=108.0, ladder_sl=105.6))
        (tr,) = e.sink.trades
        assert tr.legs == 2 and tr.reason == "TP"
        assert tr.exit == pytest.approx(0.3 * 104.0 + 0.7 * 108.0)
        assert tr.r_gross == pytest.approx((0.3 * 4.0 + 0.7 * 8.0) / 2.0)

    def test_the_stop_never_loosens(self):
        e = Env()
        self._open(e)
        e.submit(exit_sig("TP1", 0.3, fill_price=104.0, ladder_sl=102.8))
        e.submit(exit_sig("TP2", 0.5, fill_price=108.0, ladder_sl=99.0))
        assert e.pos.sl == pytest.approx(102.8)

    def test_a_raised_stop_applies_from_the_next_bar(self):
        e = Env()
        self._open(e)
        e.submit(exit_sig("TP1", 0.3, fill_price=104.0, ladder_sl=102.8))
        e.bar("10:05", 103.0, 104.0, 102.0, 103.5)
        (tr,) = e.sink.trades
        assert tr.reason == "SL" and tr.legs == 2
        assert tr.exit == pytest.approx(0.3 * 104.0 + 0.7 * 102.8)

    def test_market_exit_uses_the_last_close(self):
        e = Env()
        self._open(e)
        e.submit(exit_sig(None, 1.0))
        (tr,) = e.sink.trades
        assert tr.exit == 100.5 and tr.reason == "EXIT"

    def test_sl_reconcile_books_the_stop(self):
        e = Env()
        self._open(e)
        e.submit(exit_sig("SL", 1.0))
        (tr,) = e.sink.trades
        assert (tr.reason, tr.exit) == ("SL", 98.0)

    def test_exit_without_a_position_is_ignored(self):
        e = Env()
        e.submit(exit_sig("TP1", 1.0, fill_price=104.0))
        assert e.sink.trades == []


class TestGates:
    def test_no_second_entry_while_one_is_open_or_pending(self):
        e = Env()
        e.submit(entry())
        e.submit(entry(price=101.0))
        e.bar("10:00", 100.0, 101.0, 99.5, 100.5)
        e.submit(entry(price=102.0))
        e.bar("10:05", 100.5, 101.0, 99.5, 100.5)
        assert e.pos.sig_id == "TCS-20260930-0955" and e.pos.entry_price == 100.0

    def test_trades_per_day_cap(self):
        e = Env(max_trades_per_day=1, one_trade_per_direction=False)
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 97.5, 98.0)  # stopped on the fill bar
        e.submit(entry())
        e.bar("10:05", 100.0, 101.0, 99.5, 100.0)
        assert e.pos is None and len(e.sink.trades) == 1

    def test_one_trade_per_direction(self):
        e = Env()
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 97.5, 98.0)
        e.submit(entry())
        e.bar("10:05", 100.0, 101.0, 99.5, 100.0)
        assert e.pos is None
        e.submit(entry(Direction.SHORT, sl=102.0, tp=96.0))
        e.bar("10:10", 100.0, 100.5, 99.5, 100.0)
        assert e.pos.direction == Direction.SHORT

    def test_direction_permissions(self):
        e = Env(allow_longs=False)
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 99.5, 100.0)
        assert e.pos is None

    def test_a_new_day_resets_counters_and_drops_a_stale_pending(self):
        e = Env(max_trades_per_day=1)
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 97.5, 98.0)
        e.submit(entry())  # pending at the end of day one
        go(e.sink.on_bar("TCS", pd.Timestamp("2026-10-01 09:45", tz=IST), 100, 101, 99, 100))
        assert e.pos is None  # the pending order did not survive the session
        e.submit(entry())
        go(e.sink.on_bar("TCS", pd.Timestamp("2026-10-01 09:50", tz=IST), 100, 101, 99.5, 100))
        assert e.pos is not None  # counters were reset


class TestTimeExit:
    def test_closes_at_the_bar_close_from_the_time_exit_bar_on(self):
        e = Env()
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 99.5, 100.5)
        e.after("14:40", 101.0, 101.5, 100.5, 101.2)
        assert e.pos is not None
        e.after("14:45", 101.0, 101.5, 100.5, 101.3)
        (tr,) = e.sink.trades
        assert (tr.reason, tr.exit) == ("TIME_EXIT", 101.3)

    def test_flush_closes_leftovers_at_the_last_close(self):
        e = Env()
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 99.5, 100.5)
        e.sink.flush()
        (tr,) = e.sink.trades
        assert (tr.reason, tr.exit) == ("EOD", 100.5)
        e.sink.flush()
        assert len(e.sink.trades) == 1


class TestCosts:
    def test_net_r_charges_the_engine_cost_model(self):
        e = Env(cost_bps=16.0)
        e.submit(entry())
        e.bar("10:00", 100.0, 101.0, 99.5, 100.5)
        e.submit(exit_sig("TP1", 1.0, fill_price=104.0, ladder_sl=98.0))
        (tr,) = e.sink.trades
        assert tr.r_net == pytest.approx(2.0 - 0.0016 * 100.0 / 2.0)


class TestProtocol:
    def test_dispatches_is_false_because_no_real_order_can_go_out(self):
        assert SimSink.dispatches is False

    def test_submit_is_idempotent_for_the_pending_slot_per_symbol(self):
        e = Env()
        e.submit(entry(symbol="AAA"))
        e.submit(entry(symbol="BBB"))
        go(e.sink.on_bar("AAA", t("10:00"), 100, 101, 99.5, 100))
        go(e.sink.on_bar("BBB", t("10:00"), 100, 101, 99.5, 100))
        assert e.sink.find_position("AAA", "BREAKOUT") is not None
        assert e.sink.find_position("BBB", "BREAKOUT") is not None


class TestHasPending:
    def test_pending_from_submit_until_the_next_bar_fills_it(self):
        e = Env()
        assert not e.sink.has_pending("TCS")
        e.submit(entry())
        assert e.sink.has_pending("TCS")
        e.bar("10:00", 100.0, 101.0, 99.5, 100.5)
        assert not e.sink.has_pending("TCS")

    def test_rejected_entries_are_not_pending(self):
        e = Env(allow_longs=False)
        e.submit(entry())
        assert not e.sink.has_pending("TCS")
