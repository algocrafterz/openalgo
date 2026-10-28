"""IDEA-034 v3 shared core: the limit-entry and exit simulator on 1-minute bars.

The fill rule is the frozen v3 rule (research/ideas/idea-034-multi-day-balance-breakout/
PAPER-PLAN.md): a limit fills only when a 1m bar trades at least one tick THROUGH it, inside
the window; a touch does not fill; an unfilled order is no trade.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from signal_engine.strategy_core.balance_breakout import (
    round_to_tick,
    simulate_limit,
    simulate_market,
)

IST = "Asia/Kolkata"
DAY = "2026-11-02"
T = dt.time.fromisoformat


def _bars(rows: dict[str, tuple[float, float, float, float]], fill: float = 100.0) -> pd.DataFrame:
    """1m bars 09:15 .. 15:29, flat at `fill`, with the given HH:MM -> (O, H, L, C) overrides."""
    idx = pd.date_range(f"{DAY} 09:15", f"{DAY} 15:29", freq="1min", tz=IST)
    df = pd.DataFrame({"Open": fill, "High": fill, "Low": fill, "Close": fill}, index=idx)
    for hhmm, (o, h, lo, c) in rows.items():
        df.loc[pd.Timestamp(f"{DAY} {hhmm}", tz=IST), ["Open", "High", "Low", "Close"]] = (
            o,
            h,
            lo,
            c,
        )
    return df


def _long(df, **kw):
    args = {
        "start": T("11:15"),
        "direction": 1,
        "limit": 100.0,
        "stop": 99.0,
        "target": 102.0,
        "tick": 0.05,
    }
    args.update(kw)
    return simulate_limit(df, **args)


def test_round_to_tick():
    assert round_to_tick(100.03, 0.05) == 100.05
    assert round_to_tick(99.97, 0.05) == 99.95


def test_a_touch_of_the_limit_does_not_fill():
    df = _bars({"11:20": (100.2, 100.2, 100.0, 100.1)}, fill=100.5)
    out = _long(df)
    assert not out.filled
    assert out.gross_bps is None


def test_one_tick_through_fills_at_the_limit():
    df = _bars({"11:20": (100.2, 100.2, 99.95, 100.1)}, fill=100.5)
    out = _long(df)
    assert out.filled
    assert out.fill_price == 100.0
    assert out.fill_time == pd.Timestamp(f"{DAY} 11:20", tz=IST)


def test_a_bar_opening_through_the_limit_fills_at_its_open():
    df = _bars({"11:20": (99.8, 99.9, 99.7, 99.85)}, fill=100.5)
    assert _long(df).fill_price == 99.8


def test_no_fill_after_the_window_closes():
    df = _bars({"11:30": (100.2, 100.2, 99.5, 100.1)}, fill=100.5)  # 15 min after 11:15
    assert not _long(df).filled
    assert _long(df, window_min=30).filled


def test_bars_before_the_signal_close_are_ignored():
    df = _bars({"11:10": (99.0, 99.0, 98.0, 99.0)}, fill=100.5)
    assert not _long(df).filled


def test_the_fill_bar_reaching_the_stop_is_stopped_on_that_bar():
    df = _bars({"11:20": (100.2, 100.2, 98.9, 99.0)}, fill=100.5)
    out = _long(df)
    assert out.filled and out.reason == "SL"
    assert out.exit_price == 99.0
    assert out.exit_time == pd.Timestamp(f"{DAY} 11:20", tz=IST)


def test_the_fill_bar_does_not_take_the_target():
    df = _bars({"11:20": (100.2, 102.5, 99.9, 102.0)}, fill=100.5)
    out = _long(df)
    assert out.filled and out.reason == "TIME"  # later bars sit at 100.5, below the target


def test_target_after_the_fill_and_its_gross():
    df = _bars(
        {"11:20": (100.2, 100.2, 99.9, 100.1), "12:00": (101.5, 102.1, 101.4, 102.0)}, fill=100.5
    )
    out = _long(df)
    assert out.reason == "TP" and out.exit_price == 102.0
    assert out.gross_bps == pytest.approx(200.0)


def test_time_exit_at_14_45_open():
    df = _bars(
        {"11:20": (100.2, 100.2, 99.9, 100.1), "14:45": (100.7, 100.8, 100.6, 100.7)}, fill=100.5
    )
    out = _long(df)
    assert out.reason == "TIME" and out.exit_price == 100.7


def test_a_fill_through_the_stop_exits_flat_at_the_fill():
    df = _bars({"11:20": (98.5, 98.6, 98.4, 98.5)}, fill=100.5)
    out = _long(df)
    assert out.filled and out.reason == "SL_GAP_ENTRY"
    assert out.gross_bps == 0.0


def test_short_mirrors():
    df = _bars({"11:20": (99.8, 100.05, 99.8, 99.9), "12:00": (98.5, 98.6, 97.9, 98.0)}, fill=99.5)
    out = simulate_limit(
        df, start=T("11:15"), direction=-1, limit=100.0, stop=101.0, target=98.0, tick=0.05
    )
    assert out.filled and out.fill_price == 100.0
    assert out.reason == "TP" and out.gross_bps == pytest.approx(200.0)


def test_market_counterfactual_enters_at_the_signal_close_open():
    df = _bars(
        {"11:15": (100.5, 100.6, 100.4, 100.5), "12:00": (101.9, 102.1, 101.8, 102.0)}, fill=100.5
    )
    out = simulate_market(df, start=T("11:15"), direction=1, stop=99.0, target=102.0)
    assert out.filled and out.fill_price == 100.5
    assert out.reason == "TP"
    assert out.gross_bps == pytest.approx((102.0 / 100.5 - 1) * 1e4)


def test_later_bars_do_not_change_an_earlier_exit():
    base = {"11:20": (100.2, 100.2, 99.9, 100.1), "12:00": (101.5, 102.1, 101.4, 102.0)}
    a = _long(_bars(base, fill=100.5))
    b = _long(_bars({**base, "13:00": (90.0, 90.0, 80.0, 85.0)}, fill=100.5))
    assert a == b
