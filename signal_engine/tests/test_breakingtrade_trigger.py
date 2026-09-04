"""Trade-plan construction (trigger.py) - entry trigger, ATR-buffered stop, target ladder."""

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import trigger


def _bars(start="09:15", rows=()):
    """rows: list of (high, low, close), one per 5-minute bar from `start`."""
    base = pd.Timestamp(f"2026-09-03 {start}")
    return pd.DataFrame(
        [
            {"timestamp": base + pd.Timedelta(minutes=5 * i), "high": h, "low": low, "close": c}
            for i, (h, low, c) in enumerate(rows)
        ]
    )


def _session_bars():
    """A full IB (09:15-10:15, 12 bars) ranging 95-105, then flat bars after it."""
    return _bars("09:15", [(105, 95, 100)] * 12 + [(101, 99, 100)] * 6)


# ---------------------------------------------------------------------------
# Initial Balance
# ---------------------------------------------------------------------------


def test_initial_balance_covers_only_the_first_hour():
    bars = _bars("09:15", [(105, 95, 100)] * 12 + [(200, 50, 120)] * 4)
    high, low = trigger.initial_balance(bars)
    assert (high, low) == (105.0, 95.0)  # the 10:15+ bar must not widen it


def test_initial_balance_absent_before_the_open():
    assert trigger.initial_balance(_bars("11:00", [(105, 95, 100)])) == (None, None)


# ---------------------------------------------------------------------------
# Entry trigger
# ---------------------------------------------------------------------------


def test_long_entry_needs_a_close_above_the_signal_bar_high():
    bars = _bars("10:00", [(100, 98, 99), (101, 99, 99.5), (103, 99, 102)])
    entry, when = trigger.entry_trigger(bars, pd.Timestamp("2026-09-03 10:00"), "up")
    assert entry == 102.0  # first CLOSE above the 10:00 bar's high of 100
    assert when is not None


def test_intrabar_poke_does_not_trigger():
    """A wick through the level that closes back inside is the classic false break."""
    bars = _bars("10:00", [(100, 98, 99), (105, 99, 99.5)])
    entry, _ = trigger.entry_trigger(bars, pd.Timestamp("2026-09-03 10:00"), "up")
    assert entry is None


def test_short_entry_needs_a_close_below_the_signal_bar_low():
    bars = _bars("10:00", [(100, 98, 99), (99, 96, 97)])
    entry, _ = trigger.entry_trigger(bars, pd.Timestamp("2026-09-03 10:00"), "down")
    assert entry == 97.0


# ---------------------------------------------------------------------------
# Stop and targets
# ---------------------------------------------------------------------------


def test_long_stop_sits_below_the_ib_low_by_an_atr_buffer():
    assert trigger.stop_level("up", ib_high=105, ib_low=95, atr=4.0) == pytest.approx(95 - 1.0)


def test_short_stop_sits_above_the_ib_high():
    assert trigger.stop_level("down", ib_high=105, ib_low=95, atr=4.0) == pytest.approx(105 + 1.0)


def test_stop_is_never_exactly_on_the_structural_level():
    """A stop resting on the obvious level is resting where the hunt goes."""
    assert trigger.stop_level("up", 105, 95, 4.0) < 95


def test_targets_ladder_the_ib_range():
    assert trigger.target_levels(100.0, ib_range=10.0, direction="up") == [110.0, 115.0, 120.0]
    assert trigger.target_levels(100.0, ib_range=10.0, direction="down") == [90.0, 85.0, 80.0]


# ---------------------------------------------------------------------------
# Day-type dependent split
# ---------------------------------------------------------------------------


def test_trend_day_leaves_most_on_for_the_runner():
    split = trigger.target_split("Trend")
    assert split == trigger.TREND_SPLIT
    assert split[-1] > split[0]  # more rides than is booked at TP1


def test_normal_variation_books_early():
    split = trigger.target_split("Normal Var")
    assert split == trigger.DEFAULT_SPLIT
    assert split[0] > split[-1]


def test_every_split_allocates_the_whole_position():
    for split in (trigger.TREND_SPLIT, trigger.DEFAULT_SPLIT):
        assert sum(split) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Whole plan
# ---------------------------------------------------------------------------


def test_plan_is_none_until_the_trigger_fires():
    """Flat bars after the signal never close beyond the signal bar - no plan, no trade."""
    plan = trigger.plan_trade(
        "TESTCO", "up", "Normal Var", _session_bars(), pd.Timestamp("2026-09-03 10:15"), atr=4.0
    )
    assert plan is None


def test_plan_assembles_entry_stop_targets():
    bars = _bars("09:15", [(105, 95, 100)] * 12 + [(112, 99, 111)])
    plan = trigger.plan_trade(
        "TESTCO", "up", "Trend", bars, pd.Timestamp("2026-09-03 10:10"), atr=4.0
    )
    assert plan is not None
    assert plan.action == "BUY"
    assert plan.entry == 111.0
    assert plan.stop == pytest.approx(94.0)  # 95 - 0.25*4
    assert plan.targets == [121.0, 126.0, 131.0]  # IB range 10 -> 1x/1.5x/2x
    assert plan.split == trigger.TREND_SPLIT
    assert plan.reward_risk == pytest.approx(20 / 17, abs=0.01)


def test_plan_warns_when_the_stop_is_unusually_wide():
    bars = _bars("09:15", [(105, 95, 100)] * 12 + [(112, 99, 111)])
    plan = trigger.plan_trade(
        "TESTCO", "up", "Normal Var", bars, pd.Timestamp("2026-09-03 10:10"), atr=1.0
    )
    assert any("wider than 2 ATR" in note for note in plan.notes)


def test_plan_needs_an_atr():
    bars = _bars("09:15", [(105, 95, 100)] * 12 + [(112, 99, 111)])
    plan = trigger.plan_trade(
        "TESTCO", "up", "Trend", bars, pd.Timestamp("2026-09-03 10:10"), atr=0
    )
    assert plan is None
