"""Opt-in engine hooks: a per-bar dynamic target, the skip-at-fill rule, fill/exit callbacks.

All three are default-off. The `Default*` tests pin that a strategy which does not opt in is
simulated exactly as before (static target, SL_GAP_ENTRY on a fill through the stop).
"""

from __future__ import annotations

import pytest

from signal_engine.backtest import ledgers
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.tests.test_backtest_engine import RUN, FireOnce, bars


class Moving(FireOnce):
    """FireOnce whose target on bar j is `targets[j]`, and which records the engine callbacks."""

    dynamic_target = True

    def __init__(self, at, sl, tp, targets, direction=1):
        super().__init__(at, sl, tp, direction)
        self.targets = targets
        self.events = []
        self.asked = []

    def target(self, c, j, p, pos):
        self.asked.append(j)
        return self.targets[j]

    def on_fill(self, c, i, p, pos):
        self.events.append(("fill", i))

    def on_fill_skipped(self, c, i, p, sig):
        self.events.append(("skip", i))

    def on_exit(self, c, i, p, trade):
        self.events.append(("exit", i, trade.reason))


FLAT = (100, 101, 99, 100)


def go(rows, strat, run=RUN, rec=None):
    return simulate(Ctx(bars(rows), "T"), strat, None, run, rec)


class TestDynamicTarget:
    def test_target_moves_bar_by_bar(self):
        # entry fills bar 2 at 100; bar 2 target 105 (not hit), bar 3 target 103 (hit)
        rows = [FLAT, FLAT, (100, 104, 99, 102), (102, 104, 101, 103), FLAT]
        s = Moving(at=1, sl=90, tp=105, targets=[0, 0, 105, 103, 0])
        t = go(rows, s)
        assert len(t) == 1 and t[0].reason == "TP" and t[0].exit == 103
        assert s.asked == [2, 3]
        assert t[0].tp == 105  # the trade records the signal-time target

    def test_open_beyond_target_fills_at_open_long(self):
        rows = [FLAT, FLAT, FLAT, (106, 107, 105, 106), FLAT]
        t = go(rows, Moving(at=1, sl=90, tp=110, targets=[0, 0, 110, 104, 0]))
        assert t[0].reason == "TP" and t[0].exit == 106

    def test_open_beyond_target_fills_at_open_short(self):
        rows = [FLAT, FLAT, FLAT, (94, 95, 93, 94), FLAT]
        t = go(rows, Moving(at=1, sl=110, tp=90, targets=[0, 0, 90, 96, 0], direction=-1))
        assert t[0].reason == "TP" and t[0].exit == 94

    def test_stop_first_on_a_bar_reaching_both(self):
        rows = [FLAT, FLAT, FLAT, (100, 106, 89, 100), FLAT]
        t = go(rows, Moving(at=1, sl=90, tp=110, targets=[0, 0, 110, 104, 0]))
        assert t[0].reason == "SL" and t[0].exit == 90

    def test_callbacks_report_fill_and_exit_bars(self):
        rows = [FLAT, FLAT, (100, 104, 99, 102), (102, 104, 101, 103), FLAT]
        s = Moving(at=1, sl=90, tp=105, targets=[0, 0, 105, 103, 0])
        go(rows, s)
        assert s.events == [("fill", 2), ("exit", 3, "TP")]


class TestSkipAtFill:
    RUN_SKIP = RUN.with_(skip_fill_beyond_levels=True)

    @pytest.mark.parametrize("open_", [105, 106, 90, 85])
    def test_long_open_at_or_beyond_a_level_is_skipped(self, open_):
        rows = [FLAT, FLAT, (open_, open_ + 1, open_ - 1, open_), FLAT]
        s = Moving(at=1, sl=90, tp=105, targets=[0, 0, 105, 105])
        rec = LedgerRecorder()
        assert go(rows, s, self.RUN_SKIP, rec) == []
        assert s.events == [("skip", 2)]
        f = rec.to_frames()
        o = f["orders"].iloc[0]
        assert o["status"] == "DROPPED"
        exc = f["exclusions"]
        assert set(exc.loc[exc["scope"] == "order", "reason_code"]) == {
            ledgers.ENTRY_SKIPPED_BEYOND_LEVEL
        }
        assert rec.reconcile() == []

    def test_short_open_beyond_target_is_skipped(self):
        rows = [FLAT, FLAT, (95, 96, 94, 95), FLAT]
        s = Moving(at=1, sl=110, tp=95, targets=[0, 0, 95, 95], direction=-1)
        assert go(rows, s, self.RUN_SKIP) == []
        assert s.events == [("skip", 2)]

    def test_inside_the_levels_fills(self):
        rows = [FLAT, FLAT, FLAT, FLAT]
        s = Moving(at=1, sl=90, tp=105, targets=[0, 0, 105, 105])
        t = go(rows, s, self.RUN_SKIP)
        assert len(t) == 1 and t[0].reason == "EOD"

    def test_skipped_fill_does_not_use_a_trade_slot(self):
        class Twice(Moving):
            def entry(self, c, i, p, direction):
                if i in (1, 3) and direction == 1:
                    return EntrySignal(direction=1, sl=90, tp=105, tag="T")
                return None

        rows = [FLAT, FLAT, (106, 107, 105, 106), FLAT, FLAT, FLAT]
        s = Twice(at=1, sl=90, tp=105, targets=[105] * 6)
        t = go(rows, s, self.RUN_SKIP.with_(max_trades_per_day=1))
        assert len(t) == 1 and t[0].entry_time == bars(rows).index[4]


class TestDefaultOff:
    def test_static_target_when_not_opted_in(self):
        class NotOpted(Moving):
            dynamic_target = False

        rows = [FLAT, FLAT, FLAT, (106, 107, 105, 106), FLAT]
        s = NotOpted(at=1, sl=90, tp=105, targets=[0, 0, 110, 104, 0])
        t = go(rows, s)
        assert t[0].reason == "TP" and t[0].exit == 105  # pos.tp, not the open
        assert s.asked == []

    def test_fill_through_stop_is_still_sl_gap_entry(self):
        rows = [FLAT, FLAT, (80, 85, 78, 82), FLAT]
        s = Moving(at=1, sl=90, tp=110, targets=[110] * 4)
        t = go(rows, s)
        assert t[0].reason == "SL_GAP_ENTRY"
        assert s.events == [("fill", 2), ("exit", 2, "SL_GAP_ENTRY")]

    def test_runconfig_defaults(self):
        assert RunConfig().skip_fill_beyond_levels is False
        from signal_engine.backtest.strategies.base import Strategy

        assert Strategy.dynamic_target is False

    def test_existing_codes_keep_their_numbers(self):
        assert ledgers.REASON_CODES[: len(ledgers.LIVE_CODES)] == ledgers.LIVE_CODES
        assert ledgers.CODE_INDEX["WARMUP"] == 17
        assert ledgers.CODE_INDEX[ledgers.ENTRY_SKIPPED_BEYOND_LEVEL] == 18
