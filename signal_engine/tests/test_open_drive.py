"""Unit tests for the value-area edge scalp adapter (v2, against
`volume-profile-model.md`'s documented setups), in the same style as
`test_backtest_integrity.py`: hand-built bars, `prepare()` + `entry()` driven
directly, assertions on the arithmetic. No stateful hooks in this version - every
setup is a vectorized column, so tests call `entry()` straight after `prepare()`
with no `on_bar()`/`reset_session()` replay needed.

Every fixture uses 3+ quiet "context" sessions before the day under test, so
`prev_session_value_area()` has a real previous session's VAH/VAL/POC (pinned near
100 by construction: flat Close, tiny range) and `indicators.atr` has warmed up.
"""

from __future__ import annotations

import pandas as pd
import pytest

from signal_engine.backtest.strategies.open_drive import OpenDrive, OpenDriveParams
from signal_engine.backtest.types import Ctx

IST = "Asia/Kolkata"

#: Every setup/filter off by default; each test enables exactly what it needs.
ALL_OFF = dict(use_day_type_filter=False, use_retest=False, use_rejection=False,
               use_acceptance=False, use_clv=False, use_volume=False,
               use_vwap_filter=False)


def params(**overrides) -> OpenDriveParams:
    return OpenDriveParams(**{**ALL_OFF, **overrides})


def _bars(day: str, start: str, rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="1min", tz=IST)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def _quiet_context_days(base_price: float = 100.0, n_days: int = 3) -> list[pd.DataFrame]:
    """Flat sessions (tiny range) so prev_session_value_area pins VAH=VAL=POC=base_price
    and ATR converges to a small, predictable value."""
    out = []
    for k in range(n_days):
        rows = [(base_price, base_price + 0.05, base_price - 0.05, base_price, 50)
                for _ in range(30)]
        out.append(_bars(f"2026-06-0{k + 1}", "09:15", rows))
    return out


def _prepared_ctx(test_day_rows: list[tuple], p: OpenDriveParams, test_day="2026-06-10"):
    frames = _quiet_context_days() + [_bars(test_day, "09:15", test_day_rows)]
    df = pd.concat(frames).sort_index()
    strat = OpenDrive()
    prepared = strat.prepare(df, p)
    return strat, Ctx(prepared, "TEST")


def test_acceptance_fires_after_n_consecutive_closes_above_vah():
    rows = [
        (100.5, 101.0, 100.4, 100.9, 50),   # bar0: 1st close above VAH (100)
        (100.9, 101.2, 100.8, 101.1, 50),   # bar1: 2nd consecutive close above -> fires
    ]
    p = params(use_acceptance=True)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    assert strat.entry(c, last - 1, p, 1) is None      # only 1 close above so far
    sig = strat.entry(c, last, p, 1)
    assert sig is not None and sig.direction == 1
    assert sig.tag.startswith("ACCEPT_UP")


def test_rejection_fires_on_a_wick_below_val_that_closes_back_inside():
    # VAL is pinned at 100 by the quiet context. A wick to 95 with a close back
    # above 100 is the VAL-REJ pattern: price probed below value and was rejected
    # back up.
    rows = [(99.0, 101.5, 95.0, 101.0, 50)]
    p = params(use_rejection=True)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, 1)
    assert sig is not None
    assert sig.tag.startswith("REJECT_UP")
    # Stop must clear the WICK (95), not just the level (100) - the wick already
    # went further than the level itself.
    assert sig.sl < 95.0


def test_retest_flag_true_near_the_level_false_far_from_it():
    rows = [
        (100.5, 105.2, 100.4, 105.0, 50),   # bar0: breakout above VAH (100)
        (105.0, 105.1, 99.9, 100.02, 50),   # bar1: pulls back to essentially AT the level
        (100.02, 111.0, 99.9, 110.0, 50),   # bar2: far from the level - must NOT flag
    ]
    p = params(use_retest=True)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    assert bool(c["retest_up"][last - 1]) is True
    assert bool(c["retest_up"][last]) is False
    sig = strat.entry(c, last - 1, p, 1)
    assert sig is not None and sig.tag.startswith("RETEST_UP")


def test_clv_filter_blocks_a_weak_close_even_with_a_valid_setup():
    # Two consecutive closes above VAH (acceptance fires structurally), but the
    # trigger bar closes near the BOTTOM of its own range - a weak close the model's
    # CLV check (SS4B) is specifically meant to reject.
    rows = [
        (100.5, 101.0, 100.4, 100.9, 50),
        (102.0, 103.0, 100.6, 100.7, 50),   # wide bar, closes near its own low
    ]
    p = params(use_acceptance=True, use_clv=True)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is None
    p_no_clv = params(use_acceptance=True, use_clv=False)
    strat2, c2 = _prepared_ctx(rows, p_no_clv)
    assert strat2.entry(c2, last, p_no_clv, 1) is not None    # same bar, filter off


def test_vwap_filter_blocks_long_when_price_sits_well_below_session_vwap():
    # A huge-volume print early in the day drags session VWAP far above where the
    # (small-volume) acceptance setup later fires - a real setup location, but the
    # model's non-negotiable trend filter (rule #2) must still block it.
    rows = [
        (199.0, 201.0, 198.0, 200.0, 100_000),   # bar0: dominates VWAP
        (100.4, 100.6, 100.3, 100.5, 50),        # bar1: 2nd close above VAH(100), tiny volume
    ]
    p = params(use_acceptance=True, use_vwap_filter=True)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    assert c["Close"][last] < c["vwap"][last]
    assert strat.entry(c, last, p, 1) is None


def test_structural_target_snaps_to_the_nearest_level_ahead():
    rows = [
        (100.4, 100.5, 100.3, 100.4, 50),    # bar0: 1st close above VAH
        (100.9, 101.5, 100.8, 101.0, 50),    # bar1: 2nd close above VAH -> fires
    ]
    p = params(use_acceptance=True, tp_mode="structural")
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, 1)
    assert sig is not None
    # Candidates ahead of Close=101.0: PDH (~100, from the flat context - BEHIND,
    # not ahead) and IBH (running high of the test day itself, ~101.5 - AHEAD and
    # nearer than any context-day level). The nearest-ahead level must win.
    assert sig.tp == pytest.approx(c["ibh"][last])
    assert sig.tp < 105    # sanity: did not fall back to a wide r-multiple guess


def test_r_fallback_when_nothing_structural_lies_ahead():
    # A long Initial Balance window (60+ bars) so IBH freezes at a MODEST level
    # before the rally, then a violent rally clears every known level (PDH, IBH,
    # value area) at once - nothing is left "ahead" of price, forcing the
    # risk-multiple fallback rather than a structural target.
    ib_rows = [(100.5, 102.0, 100.0, 101.0, 50) for _ in range(61)]
    rally_rows = [
        (101.0, 500.5, 100.9, 500.0, 50),     # 1st close above VAH, far past IB/PDH
        (500.0, 501.5, 499.9, 501.0, 50),     # 2nd close above VAH -> fires
    ]
    p = params(use_acceptance=True, tp_mode="structural",
                        tp_r_fallback=1.5)
    strat, c = _prepared_ctx(ib_rows + rally_rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, 1)
    assert sig is not None
    px = c["Close"][last]
    risk = abs(px - sig.sl)
    assert sig.tp == pytest.approx(px + risk * 1.5)


def test_short_side_acceptance_mirrors_long():
    rows = [
        (99.5, 99.6, 99.0, 99.1, 50),    # bar0: 1st close below VAL (100)
        (99.1, 99.2, 98.5, 98.8, 50),    # bar1: 2nd consecutive close below -> fires
    ]
    p = params(use_acceptance=True)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, -1)
    assert sig is not None
    assert sig.direction == -1
    assert sig.tag.startswith("ACCEPT_DN")
    assert sig.sl > c["pval"][last]     # stop sits ABOVE the level for a short
