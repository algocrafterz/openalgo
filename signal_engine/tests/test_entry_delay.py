"""Engine entry delay (G4.2 latency variant): a signal on bar i fills at the open of bar i + 1 + delay.

Default 0 is the normal run. The delay is an engine argument, not a RunConfig field, so recorded
run configurations are unchanged."""

from __future__ import annotations

import pytest

from signal_engine.backtest.engine import simulate, simulate_steps
from signal_engine.backtest.types import Ctx, RunConfig
from signal_engine.tests.test_backtest_engine import RUN, FireOnce, bars

FLAT = (100, 101, 99, 100)


def go(rows, strat, delay=0, run=RUN):
    return simulate(Ctx(bars(rows), "T"), strat, None, run, entry_delay_bars=delay)


ROWS = [FLAT, FLAT, (105, 106, 104, 105), (108, 109, 107, 108), (110, 111, 109, 110), FLAT]


class TestDelayedFill:
    def test_zero_is_the_normal_run(self):
        a = go(ROWS, FireOnce(at=1, sl=90, tp=200))
        b = simulate(Ctx(bars(ROWS), "T"), FireOnce(at=1, sl=90, tp=200), None, RUN)
        assert a == b and a[0].entry == 105

    def test_one_bar_fills_at_the_open_of_the_bar_after_next(self):
        t = go(ROWS, FireOnce(at=1, sl=90, tp=200), delay=1)
        assert len(t) == 1 and t[0].entry == 108  # bar 3's open, not bar 2's
        assert t[0].entry_time == bars(ROWS).index[3]

    def test_risk_stop_and_signal_price_stay_those_of_the_signal(self):
        t = go(ROWS, FireOnce(at=1, sl=90, tp=200), delay=1)[0]
        assert t.signal_price == 100 and t.risk == pytest.approx(10.0) and t.sl == 90

    def test_the_slip_between_signal_and_fill_is_a_cost_in_r(self):
        base = go(ROWS, FireOnce(at=1, sl=90, tp=110), delay=0)[0]
        late = go(ROWS, FireOnce(at=1, sl=90, tp=110), delay=1)[0]
        assert late.entry > base.entry and late.r_gross != base.r_gross

    def test_a_fill_through_the_stop_is_taken_and_closed_like_any_other(self):
        rows = [FLAT, FLAT, (100, 101, 99, 100), (85, 86, 84, 85), FLAT]
        t = go(rows, FireOnce(at=1, sl=90, tp=200), delay=1)
        assert len(t) == 1 and t[0].entry == 85 and t[0].reason == "SL_GAP_ENTRY"

    def test_a_signal_whose_delayed_fill_falls_outside_the_session_is_not_taken(self):
        rows = [FLAT, FLAT, FLAT, FLAT]  # signal on bar 2 would fill on bar 3 (delay 0), bar 4 (delay 1)
        assert len(go(rows, FireOnce(at=2, sl=90, tp=200), delay=0)) == 1
        assert go(rows, FireOnce(at=2, sl=90, tp=200), delay=1) == []

    def test_no_second_signal_is_taken_while_the_order_is_in_flight(self):
        class Every(FireOnce):
            def entry(self, c, i, p, direction):
                if i < 1 or direction != 1:
                    return None
                from signal_engine.backtest.types import EntrySignal

                return EntrySignal(direction=1, sl=90, tp=200, tag="T")

        s = Every(at=1, sl=90, tp=200)
        t = go([FLAT] * 8, s, delay=2)
        assert len(t) == 1 and s.entries == 1

    def test_the_gated_request_carries_the_delayed_fill_time(self):
        c = Ctx(bars(ROWS), "T")
        steps = simulate_steps(c, FireOnce(at=1, sl=90, tp=200), None, RUN, gated=True, entry_delay_bars=1)
        request = next(steps)
        assert request.bar == 1 and request.entry_time == c.index[3]


class TestSquareOffGuard:
    CAP = RUN.with_(time_exit_min=9 * 60 + 30)  # the 09:30 bar is the latest exit bar

    def test_a_delayed_fill_after_the_latest_exit_bar_is_not_taken(self):
        rows = [FLAT] * 8
        # signal on the 09:25 bar: undelayed it fills on the 09:30 bar, delayed on 09:35
        assert len(go(rows, FireOnce(at=2, sl=90, tp=200), 0, self.CAP)) == 1
        assert go(rows, FireOnce(at=2, sl=90, tp=200), 1, self.CAP) == []

    def test_a_delayed_fill_on_the_latest_exit_bar_is_taken_and_exits_there(self):
        t = go([FLAT] * 8, FireOnce(at=1, sl=90, tp=200), 1, self.CAP)
        assert len(t) == 1 and t[0].reason == "TIME_EXIT"
        assert t[0].exit_time == bars([FLAT] * 8).index[3]

    def test_no_delayed_trade_ever_exits_after_the_latest_exit_bar(self):
        latest = bars([FLAT] * 8).index[3]
        for at in range(1, 7):
            for t in go([FLAT] * 8, FireOnce(at=at, sl=90, tp=200), 1, self.CAP):
                assert t.exit_time <= latest

    def test_the_normal_run_is_unchanged_by_the_guard(self):
        a = go([FLAT] * 8, FireOnce(at=2, sl=90, tp=200), 0, self.CAP)
        assert len(a) == 1 and a[0].exit_time == bars([FLAT] * 8).index[3]


class TestArgument:
    @pytest.mark.parametrize("bad", [-1, 1.5, True, "1", None])
    def test_it_must_be_a_non_negative_integer(self, bad):
        with pytest.raises(ValueError, match="entry_delay_bars"):
            go(ROWS, FireOnce(at=1, sl=90, tp=200), delay=bad)

    def test_it_is_not_a_run_config_field(self):
        assert "entry_delay_bars" not in RunConfig.__dataclass_fields__
