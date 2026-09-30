"""The shared backtest engine runs the ladder when RunConfig.exit_plan is set.

Contract: exit_plan=None is the legacy single-stop/single-target path, untouched. A one-leg
plan with no stop move must reproduce it exactly, so any difference in a ladder run is the
ladder and nothing else. Costs are charged on the entry notional, unchanged by partial legs.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import EntrySignal, RunConfig
from signal_engine.strategy_core.exits import ExitPlan

IST = "Asia/Kolkata"
RISK = 1.0


class Stub(Strategy):
    """Fires a long on chosen bar positions: stop 1R below, targets at 1R / 2R / 3R."""

    tag = "STUB"

    def __init__(self, fire_at):
        self.fire_at = set(fire_at)

    def prepare(self, df, p):
        d = ind.add_session_columns(df)
        d["fire"] = [1.0 if i in self.fire_at else 0.0 for i in range(len(d))]
        return d

    def entry(self, c, i, p, direction):
        if direction != 1 or not c["fire"][i]:
            return None
        px = float(c["Close"][i])
        targets = (px + RISK, px + 2 * RISK, px + 3 * RISK)
        return EntrySignal(1, px - RISK, targets[0], "STUB", meta={"targets": targets})


def bars(rows, start="10:00", day="2026-09-29"):
    """rows are (open, high, low, close)."""
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="5min", tz=IST)
    o, h, lo, c = (list(x) for x in zip(*rows, strict=True))
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1000.0}, index=idx)


RUN = RunConfig(
    skip_open_minutes=0,
    entry_cutoff_min=15 * 60,
    max_trades_per_day=99,
    one_trade_per_direction=False,
)
FLAT = (100.0, 100.0, 100.0, 100.0)


def run(df, fire_at, plan=None, **over):
    cfg = RUN.with_(exit_plan=plan, **over)
    return Backtest(Stub(fire_at), {"SYM": df}, cfg).trades(object(), "all")


class TestEquivalence:
    def test_single_leg_plan_reproduces_the_legacy_path_exactly(self):
        rng = np.random.default_rng(11)
        frames = []
        for day in ("2026-09-24", "2026-09-25", "2026-09-28"):
            close = 100 + np.cumsum(rng.normal(0, 0.6, 60))
            rows = [
                (
                    c0,
                    max(c0, c1) + abs(rng.normal(0, 0.3)),
                    min(c0, c1) - abs(rng.normal(0, 0.3)),
                    c1,
                )
                for c0, c1 in zip(np.r_[100.0, close[:-1]], close, strict=True)
            ]
            frames.append(bars(rows, day=day))
        df = pd.concat(frames)
        fires = range(3, len(df), 7)
        legacy = run(df, fires, plan=None)
        ladder = run(df, fires, plan=ExitPlan(fractions=(1.0,)))
        assert len(legacy) == len(ladder) > 10
        for a, b in zip(legacy, ladder, strict=True):
            assert (a.day, a.entry_time, a.exit_time) == (b.day, b.entry_time, b.exit_time)
            assert a.entry == pytest.approx(b.entry) and a.exit == pytest.approx(b.exit)
            assert a.r_gross == pytest.approx(b.r_gross) and a.r_net == pytest.approx(b.r_net)
            assert a.reason == b.reason


class TestBrokerSquareOffGuard:
    """Broker MIS square-off is 15:00 IST (Flattrade). No backtest trade may still be open then:
    the bar that closes the position must itself close at least the margin before 15:00."""

    def late_rows(self):
        return [FLAT] * 20  # 14:00 .. 15:35, flat, so neither stop nor target is ever hit

    def exit_stamp(self, trades):
        return trades[0].exit_time.strftime("%H:%M")

    @pytest.mark.parametrize("plan", [None, ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven")])
    def test_a_15_00_time_exit_is_capped_so_the_exit_bar_closes_before_15_00(self, plan):
        trades = run(bars(self.late_rows(), start="14:00"), [1], plan, time_exit_min=15 * 60)
        assert self.exit_stamp(trades) == "14:50"  # bar 14:50-14:55, five minutes of margin
        assert trades[0].reason == "TIME_EXIT"

    @pytest.mark.parametrize("plan", [None, ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven")])
    def test_the_default_14_45_time_exit_is_unchanged(self, plan):
        trades = run(bars(self.late_rows(), start="14:00"), [1], plan)
        assert self.exit_stamp(trades) == "14:45"

    def test_no_new_entry_is_taken_after_the_capped_time_exit(self):
        trades = run(bars(self.late_rows(), start="14:00"), [8, 9, 10], None, time_exit_min=15 * 60)
        assert all(t.entry_time.strftime("%H:%M") < "14:50" for t in trades)

    def test_a_later_broker_square_off_is_honoured_when_configured(self):
        trades = run(
            bars(self.late_rows(), start="14:00"),
            [1],
            None,
            time_exit_min=15 * 60 + 10,
            mis_squareoff_min=15 * 60 + 15,
        )
        assert self.exit_stamp(trades) == "15:05"  # closes 15:10, five minutes before 15:15


class TestTwoLegTrade:
    def rows(self):
        return [
            FLAT,
            FLAT,
            FLAT,
            FLAT,
            (100.0, 101.2, 100.1, 101.0),  # TP1: half off at 101
            (100.4, 100.6, 99.8, 100.2),  # breakeven stop at 100 takes the rest
            FLAT,
            FLAT,
        ]

    def test_half_banked_then_stopped_at_breakeven(self):
        plan = ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven")
        (t,) = run(bars(self.rows()), [1], plan)
        assert t.legs == 2 and t.reason == "SL"
        assert t.r_gross == pytest.approx(0.5)  # 0.5 * (101 - 100) + 0.5 * 0, over 1R
        assert t.exit == pytest.approx(100.5)  # size-weighted average exit

    def test_costs_are_charged_once_on_the_entry_notional(self):
        plan = ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven")
        (t,) = run(bars(self.rows()), [1], plan, cost_bps=16.0)
        assert t.r_gross - t.r_net == pytest.approx(0.0016 * t.entry / t.risk)

    def test_time_exit_closes_the_remaining_leg_at_the_close(self):
        rows = [
            FLAT,
            FLAT,
            FLAT,
            (100.0, 101.2, 100.1, 101.0),  # TP1 half at 101, stop to breakeven
            (101.0, 101.1, 100.2, 100.5),  # nothing hit; time exit at this close
            FLAT,
            FLAT,
        ]
        plan = ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven")
        (t,) = run(bars(rows), [1], plan, time_exit_min=10 * 60 + 20)
        assert t.reason == "TIME_EXIT" and t.legs == 2
        assert t.r_gross == pytest.approx(0.5 * 1.0 + 0.5 * 0.5)

    def test_entry_gapping_through_the_stop_is_flat_like_the_legacy_path(self):
        rows = [FLAT, FLAT, FLAT, (98.5, 98.6, 98.0, 98.2), FLAT, FLAT]
        plan = ExitPlan(fractions=(0.5, 0.5), stop_rule="breakeven")
        legacy = run(bars(rows), [2], None)  # signal on bar 2, so the fill is bar 3's gapped open
        laddered = run(bars(rows), [2], plan)
        assert [t.reason for t in laddered] == [t.reason for t in legacy] == ["SL_GAP_ENTRY"]
        assert laddered[0].r_gross == pytest.approx(legacy[0].r_gross)

    def test_legacy_trades_report_a_single_leg(self):
        (t,) = run(bars(self.rows()), [1], None)
        assert t.legs == 1
