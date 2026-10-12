"""VWAP rejection signal family: detection mirrors the v2 .lipi rules, measurement is forward-only."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_engine.analysis import vwap_rejection as vr

P = vr.RejectionParams()


def _detect(disp, close=None, day=None):
    disp = np.asarray(disp, dtype=float)
    n = len(disp)
    if close is None:
        # price follows displacement: falling disp = falling close
        close = 100.0 + disp
    if day is None:
        day = np.zeros(n, dtype=int)
    return vr.detect(disp, np.asarray(close, dtype=float), np.asarray(day), P)


# TCS 08-Oct shape: deep below VWAP, back to about zero, rolls over without crossing.
BEAR_TCS = [-1.0, -0.8, -0.5, -0.3, -0.2, -0.05, -0.1, -0.2, -0.4]


def test_baseline_parameters_are_the_v2_values():
    assert (P.depth, P.zone, P.move) == (0.75, 0.25, 0.10)


def test_bearish_rejection_fires_once_at_the_pullback_bar():
    events = _detect(BEAR_TCS)
    assert len(events) == 1
    e = events[0]
    assert (e.k, e.direction, e.reason) == (7, -1, vr.VWAP_REJECTION_BEARISH)
    assert e.prior_extreme == pytest.approx(-1.0)
    assert e.zone_extreme == pytest.approx(-0.05)


def test_bullish_rejection_is_the_exact_mirror():
    events = _detect([-d for d in BEAR_TCS])
    assert len(events) == 1
    e = events[0]
    assert (e.k, e.direction, e.reason) == (7, 1, vr.VWAP_REJECTION_BULLISH)
    assert e.prior_extreme == pytest.approx(1.0)
    assert e.zone_extreme == pytest.approx(0.05)


def test_shallow_prior_leg_does_not_qualify():
    assert _detect([-0.7, -0.5, -0.2, -0.05, -0.2, -0.4]) == []


def test_full_cross_beyond_zone_cancels_the_setup():
    assert _detect([-1.0, -0.5, -0.1, 0.3, 0.1, -0.1, -0.3]) == []


def test_shallow_penetration_inside_zone_still_counts():
    events = _detect([-1.0, -0.5, -0.1, 0.2, 0.05, -0.1])
    assert [e.k for e in events] == [4]
    assert events[0].zone_extreme == pytest.approx(0.2)


def test_close_confirmation_is_required():
    disp = BEAR_TCS
    close = 100.0 + np.asarray(disp)
    close[7] = close[6] + 0.5  # price up on the pullback bar
    events = _detect(disp, close)
    # fires on the next bar instead, where displacement and price both fall
    assert [e.k for e in events] == [8]


def test_one_signal_per_approach_and_rearm_after_new_deep_leg():
    disp = BEAR_TCS + [-0.9, -0.5, -0.2, -0.1, -0.25, -0.5]
    events = _detect(disp)
    assert [e.k for e in events] == [7, 13]


def test_no_rearm_without_a_new_deep_leg():
    disp = BEAR_TCS + [-0.3, -0.1, -0.25, -0.4]
    assert [e.k for e in _detect(disp)] == [7]


def test_state_resets_each_session():
    disp = [-1.0, -0.9, -0.5, -0.1, -0.05, -0.2, -0.4]
    day = [0, 0, 1, 1, 1, 1, 1]
    assert _detect(disp, day=day) == []


def test_nan_displacement_leaves_state_untouched():
    disp = [np.nan, -1.0, -0.5, np.nan, -0.05, -0.2]
    assert [e.k for e in _detect(disp)] == [5]


# ---- measurement ---------------------------------------------------------------------------


def _inputs(disp, close, high=None, low=None):
    n = len(disp)
    idx = pd.date_range("2026-10-08 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 0.5 if high is None else high,
            "Low": close - 0.5 if low is None else low,
            "Close": close,
            "disp": np.asarray(disp, dtype=float),
            "vwap": close + 1.0,
            "ema_s": close + 2.0,
            "atr": 2.0,
        },
        index=idx,
    )


def _first(inp):
    day = np.asarray(inp.index.date)
    return vr.detect(inp["disp"].to_numpy(), inp["Close"].to_numpy(), day, P)[0]


CLOSE_11 = [100, 101, 102, 103, 104, 105, 104, 103, 102, 101, 100]


def test_measure_records_signal_time_diagnostics():
    inp = _inputs(BEAR_TCS + [-0.6, -0.8], CLOSE_11)
    row = vr.measure(inp, _first(inp), P, "TCS")
    assert row["symbol"] == "TCS"
    assert row["family"] == vr.VWAP_REJECTION
    assert row["reason_code"] == vr.VWAP_REJECTION_BEARISH
    assert row["direction"] == -1
    assert row["session_date"] == "2026-10-08"
    assert row["signal_time"] == inp.index[7]
    assert row["signal_price"] == 103
    assert row["prior_extreme"] == pytest.approx(-1.0)
    assert row["zone_extreme"] == pytest.approx(-0.05)
    assert row["disp_at_signal"] == pytest.approx(-0.2)
    assert row["dist_from_zero"] == pytest.approx(0.2)
    assert row["pullback"] == pytest.approx(0.15)
    assert row["price_vwap_atr"] == pytest.approx(-0.5)
    assert row["price_ema20_atr"] == pytest.approx(-1.0)


def test_measure_forward_excursions_and_success():
    high = np.asarray(CLOSE_11, dtype=float) + 0.5
    high[8] = 104.0  # adverse 1.0 above the 103 signal close on the first forward bar
    inp = _inputs(BEAR_TCS + [-0.6, -0.8], CLOSE_11, high=high)
    row = vr.measure(inp, _first(inp), P, "TCS")
    # short from 103, atr 2: best low 99.5 at bar 10 -> 3.5/2; worst high 104 at bar 8 -> 1/2
    assert row["mfe_atr"] == pytest.approx(1.75)
    assert row["bars_to_mfe"] == 3
    assert row["mae_atr"] == pytest.approx(0.5)
    assert row["bars_to_mae"] == 1
    assert row["forward_bars"] == 3
    assert row["crossover_after"] is False
    assert row["crossover_dir"] == 0
    assert np.isnan(row["bars_to_crossover"])
    # displacement re-extends to -0.8 <= -0.75 at bar 10 before any upward cross
    assert row["outcome"] == vr.SUCCESS
    assert row["success"] is True
    assert row["bars_to_outcome"] == 3


def test_crossover_after_is_kept_without_relabelling_the_event():
    close = [100, 101, 102, 103, 104, 105, 104, 103, 102, 103, 104, 105]
    inp = _inputs(BEAR_TCS + [-0.1, 0.2, 0.4], close)
    row = vr.measure(inp, _first(inp), P, "TCS")
    assert row["reason_code"] == vr.VWAP_REJECTION_BEARISH
    assert row["crossover_after"] is True
    assert row["crossover_dir"] == 1
    assert row["bars_to_crossover"] == 3
    assert row["outcome"] == vr.FAILED
    assert row["success"] is False
    assert row["bars_to_outcome"] == 3


def test_crossover_in_the_rejection_direction_does_not_fail_it():
    # shallow penetration: signal at +0.05, then displacement crosses below zero (in favour)
    disp = [-1.0, -0.5, -0.1, 0.2, 0.05, -0.1, -0.3]
    close = [100, 101, 102, 103, 102, 101, 100]
    inp = _inputs(disp, close)
    row = vr.measure(inp, _first(inp), P, "TCS")
    assert row["crossover_after"] is True
    assert row["crossover_dir"] == -1
    assert row["bars_to_crossover"] == 1
    assert row["outcome"] == vr.UNRESOLVED


def test_unresolved_when_session_ends_first():
    inp = _inputs(BEAR_TCS + [-0.5], CLOSE_11[:10])
    row = vr.measure(inp, _first(inp), P, "TCS")
    assert row["outcome"] == vr.UNRESOLVED
    assert row["success"] is None
    assert np.isnan(row["bars_to_outcome"])


def test_forward_window_stops_at_session_end():
    close = [100, 101, 102, 103, 104, 105, 104, 103, 102, 90]
    inp = _inputs(BEAR_TCS + [-0.8], close)
    # the last bar belongs to the next session: its drop and deep displacement must not count
    next_open = pd.DatetimeIndex([pd.Timestamp("2026-10-09 09:15", tz="Asia/Kolkata")])
    inp.index = inp.index[:-1].append(next_open)
    row = vr.measure(inp, _first(inp), P, "TCS")
    assert row["forward_bars"] == 1
    assert row["mfe_atr"] == pytest.approx((103 - 101.5) / 2)
    assert row["outcome"] == vr.UNRESOLVED


def test_no_forward_bars_gives_nan_excursions():
    inp = _inputs(BEAR_TCS[:8], CLOSE_11[:8])
    row = vr.measure(inp, _first(inp), P, "TCS")
    assert row["forward_bars"] == 0
    assert np.isnan(row["mfe_atr"]) and np.isnan(row["mae_atr"])
    assert np.isnan(row["bars_to_mfe"]) and np.isnan(row["bars_to_mae"])


def test_events_for_symbol_uses_strategy_indicator_inputs(monkeypatch):
    inp = _inputs(BEAR_TCS + [-0.8], CLOSE_11[:10])
    monkeypatch.setattr(vr, "build_inputs", lambda df, p: inp)
    rows = vr.events_for_symbol("TCS", inp, P)
    assert len(rows) == 1 and rows[0]["reason_code"] == vr.VWAP_REJECTION_BEARISH


def test_summary_groups_by_reason_and_outcome():
    rows = pd.DataFrame(
        {
            "reason_code": [vr.VWAP_REJECTION_BEARISH] * 2 + [vr.VWAP_REJECTION_BULLISH],
            "outcome": [vr.SUCCESS, vr.FAILED, vr.SUCCESS],
            "mfe_atr": [1.0, 0.2, 0.8],
            "mae_atr": [0.3, 0.9, 0.1],
        }
    )
    s = vr.summarize(rows)
    assert s[vr.VWAP_REJECTION_BEARISH]["n"] == 2
    assert s[vr.VWAP_REJECTION_BEARISH]["outcomes"] == {vr.SUCCESS: 1, vr.FAILED: 1}
    assert s[vr.VWAP_REJECTION_BULLISH]["median_mfe_atr"] == pytest.approx(0.8)
