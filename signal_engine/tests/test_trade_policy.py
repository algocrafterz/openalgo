"""strategy_core.trade_policy: the shared entry rules (spec items 15 and 16)."""

import datetime as dt
import itertools
from types import SimpleNamespace

import pytest

from signal_engine.strategy_core import trade_policy as tp

DAY = dt.date(2026, 10, 8)


def run(**kw):
    base = {"one_trade_per_direction": True, "max_trades_per_day": 2, "allow_longs": True, "allow_shorts": True}
    base.update(kw)
    return SimpleNamespace(**base)


def old_engine_verdict(r, trades, long_taken, short_taken, d):
    """The inline rule the engine used before the policy module (kept here as the reference)."""
    if trades >= r.max_trades_per_day:
        return tp.MAX_TRADES_DAY
    if d == 1 and (not r.allow_longs or (r.one_trade_per_direction and long_taken)):
        return tp.DIRECTION_DISABLED if not r.allow_longs else tp.DIRECTION_ALREADY_TAKEN
    if d == -1 and (not r.allow_shorts or (r.one_trade_per_direction and short_taken)):
        return tp.DIRECTION_DISABLED if not r.allow_shorts else tp.DIRECTION_ALREADY_TAKEN
    return tp.OK


class TestPolicyReproducesTheEngineRulesItReplaced:
    @pytest.mark.parametrize(
        "one,cap,al,ash,trades,lt,st,d",
        list(
            itertools.product(
                (True, False), (0, 1, 2, 5), (True, False), (True, False), (0, 1, 2, 5), (False, True), (False, True), (1, -1)
            )
        ),
    )
    def test_every_combination_matches_the_old_inline_rule(self, one, cap, al, ash, trades, lt, st, d):
        r = run(one_trade_per_direction=one, max_trades_per_day=cap, allow_longs=al, allow_shorts=ash)
        p = tp.TradePolicy.from_run(r)
        # the engine tests the daily cap first, then the direction rules
        got = tp.trades_capped(p, trades) or tp.direction_verdict(p, d, int(lt), int(st))
        assert got == old_engine_verdict(r, trades, lt, st, d)


class TestReentryAfterFlat:
    p = tp.TradePolicy(max_entries_per_direction_per_day=2, max_trades_per_day=None)

    def test_long_exit_long_is_allowed_and_a_third_long_is_not(self):
        s = tp.roll(None, DAY)
        assert tp.can_arm(self.p, s, 1) == tp.OK
        s = tp.on_flat(tp.on_fill(tp.on_arm(s), 1))
        assert tp.can_arm(self.p, s, 1) == tp.OK  # LONG, exit, LONG
        s = tp.on_flat(tp.on_fill(tp.on_arm(s), 1))
        assert tp.can_arm(self.p, s, 1) == tp.DIRECTION_ALREADY_TAKEN

    def test_the_short_side_has_its_own_count(self):
        s = tp.roll(None, DAY)
        for _ in range(2):
            s = tp.on_flat(tp.on_fill(tp.on_arm(s), 1))
        assert tp.can_arm(self.p, s, -1) == tp.OK

    def test_no_second_entry_while_a_position_is_open(self):
        s = tp.on_fill(tp.on_arm(tp.roll(None, DAY)), 1)
        assert tp.can_arm(self.p, s, 1) == tp.POSITION_OPEN
        assert tp.can_arm(self.p, s, -1) == tp.POSITION_OPEN  # a reversal needs flat

    def test_no_second_entry_while_an_order_is_pending(self):
        s = tp.on_arm(tp.roll(None, DAY))
        assert tp.can_arm(self.p, s, 1) == tp.POSITION_OPEN

    def test_reversal_is_allowed_once_flat(self):
        s = tp.on_flat(tp.on_fill(tp.on_arm(tp.roll(None, DAY)), 1))
        assert tp.can_arm(self.p, s, -1) == tp.OK


class TestCountersCountActualFillsOnly:
    p = tp.TradePolicy(max_entries_per_direction_per_day=1)

    def test_an_order_that_never_fills_uses_nothing(self):
        s = tp.on_unfilled(tp.on_arm(tp.roll(None, DAY)))
        assert (s.long_fills, s.trades, s.pending) == (0, 0, False)
        assert tp.can_arm(self.p, s, 1) == tp.OK

    def test_a_declined_signal_never_touches_state(self):
        s = tp.roll(None, DAY)
        tp.can_arm(self.p, s, 1)  # a verdict only, the value is immutable
        assert s == tp.roll(None, DAY)

    def test_a_fill_closed_at_once_still_counts_as_used(self):
        # the bad-fill close (item 18): filled, flat again, entry still consumed
        s = tp.on_flat(tp.on_fill(tp.on_arm(tp.roll(None, DAY)), 1))
        assert s.long_fills == 1 and s.open_direction == 0
        assert tp.can_arm(self.p, s, 1) == tp.DIRECTION_ALREADY_TAKEN


class TestDayRoll:
    def test_a_new_day_resets_every_counter_and_drops_the_pending_order(self):
        s = tp.on_arm(tp.on_flat(tp.on_fill(tp.on_arm(tp.roll(None, DAY)), 1)))
        nxt = tp.roll(s, DAY + dt.timedelta(days=1))
        assert (nxt.long_fills, nxt.trades, nxt.pending, nxt.open_direction) == (0, 0, False, 0)

    def test_the_same_day_keeps_the_state(self):
        s = tp.on_fill(tp.on_arm(tp.roll(None, DAY)), -1)
        assert tp.roll(s, DAY) is s


class TestCaps:
    def test_no_daily_cap_when_none(self):
        assert tp.trades_capped(tp.TradePolicy(max_trades_per_day=None), 99) == tp.OK

    def test_a_cap_of_zero_blocks_every_entry(self):
        assert tp.trades_capped(tp.TradePolicy(max_trades_per_day=0), 0) == tp.MAX_TRADES_DAY

    def test_direction_off_wins_over_the_count(self):
        p = tp.TradePolicy(allow_shorts=False, max_entries_per_direction_per_day=1)
        assert tp.direction_verdict(p, -1, 0, 5) == tp.DIRECTION_DISABLED
