"""Shared exit model: partial targets with stop moves, stepped one bar at a time.

Long scenario used throughout: entry 100, stop 99 (1R), targets 101 / 102 / 103.
Conventions match the shared engine: the stop is checked first, a gap through the stop fills
at the open, and a raised stop applies from the NEXT bar unless same_bar_stop is set.
"""

import pytest

from signal_engine.strategy_core.exits import ExitPlan, Ladder

T3 = (101.0, 102.0, 103.0)


def long_ladder(plan, entry=100.0, sl=99.0, targets=T3):
    return Ladder(1, entry, sl, targets, plan)


def prices(lad):
    return [(f.reason, f.price, round(f.frac, 6)) for f in lad.fills]


class TestBaselineSingleTarget:
    def test_target_fills_everything(self):
        lad = long_ladder(ExitPlan(fractions=(1.0,)))
        lad.step(100.0, 101.2, 99.8)
        assert prices(lad) == [("TP1", 101.0, 1.0)]
        assert lad.done

    def test_stop_wins_when_a_bar_spans_both(self):
        lad = long_ladder(ExitPlan(fractions=(1.0,)))
        lad.step(100.0, 101.5, 98.9)
        assert prices(lad) == [("SL", 99.0, 1.0)]

    def test_gap_through_the_stop_fills_at_the_open(self):
        lad = long_ladder(ExitPlan(fractions=(1.0,)))
        lad.step(98.5, 99.0, 98.0)
        assert prices(lad) == [("SL_GAP", 98.5, 1.0)]

    def test_quiet_bar_changes_nothing(self):
        lad = long_ladder(ExitPlan(fractions=(1.0,)))
        lad.step(100.0, 100.5, 99.5)
        assert not lad.fills and not lad.done and lad.remaining == pytest.approx(1.0)


class TestPartialsAndStopMoves:
    def test_breakeven_after_first_leg_then_stopped_at_entry(self):
        lad = long_ladder(ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven"))
        lad.step(100.0, 101.2, 100.1)
        assert prices(lad) == [("TP1", 101.0, 0.5)]
        assert lad.sl_eff == 100.0
        lad.step(100.3, 100.4, 99.9)
        assert prices(lad)[-1] == ("SL", 100.0, 0.5)
        assert lad.done

    def test_tp_buffer_puts_the_stop_a_fraction_of_the_target_distance_below_it(self):
        lad = long_ladder(ExitPlan(fractions=(0.5, 0.5), stop_rule="tp_buffer", buffer_r=0.3))
        lad.step(100.0, 101.2, 100.9)
        assert lad.sl_eff == pytest.approx(101.0 - 0.3 * 1.0)

    def test_raised_stop_waits_for_the_next_bar_by_default(self):
        plan = ExitPlan(fractions=(0.5, 0.5), stop_rule="tp_buffer", buffer_r=0.3)
        lad = long_ladder(plan)
        lad.step(100.0, 101.2, 100.2)  # low 100.2 is below the new 100.7 stop
        assert not lad.done and lad.remaining == pytest.approx(0.5)

    def test_same_bar_stop_option_is_the_pessimistic_read(self):
        plan = ExitPlan(
            fractions=(0.5, 0.5), stop_rule="tp_buffer", buffer_r=0.3, same_bar_stop=True
        )
        lad = long_ladder(plan)
        lad.step(100.0, 101.2, 100.2)
        reason, price, frac = prices(lad)[-1]
        assert (reason, frac) == ("SL", 0.5)
        assert price == pytest.approx(100.7)
        assert lad.done

    def test_a_gap_through_several_targets_fills_them_in_order(self):
        lad = long_ladder(ExitPlan(fractions=(0.3, 0.35, 0.35), stop_rule="tp_buffer"))
        lad.step(100.0, 103.5, 100.0)
        assert prices(lad) == [("TP1", 101.0, 0.3), ("TP2", 102.0, 0.35), ("TP3", 103.0, 0.35)]
        assert lad.done

    def test_zero_fraction_leg_moves_the_stop_without_selling(self):
        lad = long_ladder(ExitPlan(fractions=(0.0, 1.0), stop_rule="breakeven"))
        lad.step(100.0, 101.2, 100.1)
        assert not lad.fills and lad.remaining == pytest.approx(1.0)
        assert lad.sl_eff == 100.0

    def test_the_stop_never_loosens(self):
        lad = long_ladder(ExitPlan(fractions=(0.3, 0.35, 0.35), stop_rule="breakeven"), sl=100.4)
        lad.step(100.5, 101.2, 100.45)
        assert lad.sl_eff == 100.4  # breakeven (100.0) would loosen it

    def test_fractions_below_one_leave_a_runner_on_the_stop(self):
        lad = long_ladder(ExitPlan(fractions=(0.5,), stop_rule="breakeven"))
        lad.step(100.0, 101.2, 100.1)
        assert lad.remaining == pytest.approx(0.5) and not lad.done
        lad.step(100.2, 100.3, 99.9)
        assert prices(lad)[-1] == ("SL", 100.0, 0.5)

    def test_forced_exit_takes_whatever_remains(self):
        lad = long_ladder(ExitPlan(fractions=(0.5, 0.5)))
        lad.step(100.0, 101.2, 100.1)
        lad.close_all(100.6, "TIME_EXIT")
        assert prices(lad)[-1] == ("TIME_EXIT", 100.6, 0.5)
        assert lad.done
        lad.close_all(100.9, "EOD")  # already done, no double exit
        assert len(lad.fills) == 2

    def test_fills_always_sum_to_the_whole_position(self):
        lad = long_ladder(ExitPlan(fractions=(0.3, 0.35, 0.35), stop_rule="tp_buffer"))
        for o, h, low in [(100, 101.1, 100.5), (100.8, 102.1, 100.9), (101.5, 101.6, 100.5)]:
            lad.step(o, h, low)
        assert sum(f.frac for f in lad.fills) == pytest.approx(1.0)


class TestShort:
    def test_mirror_image(self):
        lad = Ladder(
            -1, 100.0, 101.0, (99.0, 98.0), ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven")
        )
        lad.step(100.0, 99.9, 98.8)
        assert prices(lad) == [("TP1", 99.0, 0.5)]
        assert lad.sl_eff == 100.0
        lad.step(99.5, 100.1, 99.4)
        assert prices(lad)[-1] == ("SL", 100.0, 0.5)


class TestPlanValidation:
    def test_fractions_may_not_exceed_the_position(self):
        with pytest.raises(ValueError, match="fractions"):
            ExitPlan(fractions=(0.6, 0.6))

    def test_unknown_stop_rule_is_rejected(self):
        with pytest.raises(ValueError, match="stop_rule"):
            ExitPlan(fractions=(1.0,), stop_rule="trail")

    def test_more_fractions_than_targets_is_rejected(self):
        with pytest.raises(ValueError, match="targets"):
            Ladder(1, 100.0, 99.0, (101.0,), ExitPlan(fractions=(0.5, 0.5)))
