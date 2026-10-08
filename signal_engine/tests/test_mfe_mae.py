"""MFE/MAE diagnostic for IDEA-032: excursions measured from the executable fill (open of i+1).

Synthetic one-session frames carry hand-set displacement so every number is checkable. A 5-minute
session has 75 bars from 09:15 (index j opens at 555 + 5j minutes); the engine time exit bar is the
14:45 bar (j = 66) and the last signal bar opens at 14:20 (j = 61).
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import indicators as ind
from signal_engine.backtest import mfe_mae as mm
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.ema_vwap_atr_displacement import (
    EmaVwapAtrDisplacement,
    EmaVwapAtrDisplacementParams,
)
from signal_engine.backtest.types import Ctx, RunConfig

IST = "Asia/Kolkata"
P = EmaVwapAtrDisplacementParams()
S = EmaVwapAtrDisplacement()
N = 75
RULES = mm.WindowRules(
    warmup_bars=0, entry_cutoff_min=14 * 60 + 25, skip_open_minutes=0, time_exit_min=14 * 60 + 45
)


def build(days=("2026-06-01",), disp=None, opens=None, highs=None, lows=None, closes=None, atr=1.0):
    """A prepared frame (OHLC, atr, disp, disp_prev) over whole 75-bar sessions."""
    idx = []
    for d in days:
        base = pd.Timestamp(d, tz=IST) + pd.Timedelta(hours=9, minutes=15)
        idx += [base + pd.Timedelta(minutes=5 * k) for k in range(N)]
    n = len(idx)
    flat = np.full(n, 100.0)
    df = pd.DataFrame(
        {
            "Open": flat.copy() if opens is None else np.asarray(opens, float),
            "High": flat + 0.0 if highs is None else np.asarray(highs, float),
            "Low": flat + 0.0 if lows is None else np.asarray(lows, float),
            "Close": flat.copy() if closes is None else np.asarray(closes, float),
            "Volume": np.full(n, 1000.0),
        },
        index=pd.DatetimeIndex(idx),
    )
    d = ind.add_session_columns(df)
    d["atr"] = atr
    dv = np.full(n, -0.3) if disp is None else np.asarray(disp, float)
    d["disp"] = dv
    same = d["day"].eq(d["day"].shift(1)).to_numpy()
    d["disp_prev"] = d["disp"].shift(1).where(same)
    return d


def long_path(sig=10, zero=15, end_level=0.1):
    """Displacement -0.6 until the signal bar, -0.4 until the zero bar, then end_level."""
    disp = np.full(N, -0.6)
    disp[sig:] = -0.4
    disp[zero:] = end_level
    return disp


def test_window_rules_follow_engine_time_exit():
    run = RunConfig()
    assert mm.effective_time_exit_min(run, 5) == 14 * 60 + 45
    late = RunConfig(time_exit_min=15 * 60 + 20)
    assert mm.effective_time_exit_min(late, 5) == 15 * 60 - 5 - 5


def test_signal_gates_warmup_cutoff_and_session_edges():
    # a long signal at j=3, j=61 (last allowed), j=62 (cutoff), plus the first bar of the session
    disp = np.full(N, -0.6)
    for j in (3, 61, 62):
        disp[j] = -0.4
        disp[j + 1] = -0.6
    c = Ctx(build(disp=disp), symbol="X")
    got = mm.find_signals(c, S, P, RULES)
    assert [(i, d) for i, d in got] == [(3, 1), (61, 1)]
    warm = mm.WindowRules(10, RULES.entry_cutoff_min, 0, RULES.time_exit_min)
    assert [(i, d) for i, d in mm.find_signals(c, S, P, warm)] == [(61, 1)]


def test_no_signal_on_first_bar_or_last_bar_of_session():
    d1 = np.full(N, -0.6)
    d1[0] = -0.4  # first bar has no previous displacement in its session
    c = Ctx(build(disp=d1), symbol="X")
    assert mm.find_signals(c, S, P, RULES) == []
    two = build(days=("2026-06-01", "2026-06-02"))
    disp = np.full(2 * N, -0.6)
    disp[N] = -0.4  # first bar of day 2: previous bar is another session
    two["disp"] = disp
    two["disp_prev"] = (
        pd.Series(disp).shift(1).where(two["day"].eq(two["day"].shift(1)).to_numpy()).to_numpy()
    )
    assert mm.find_signals(Ctx(two, symbol="X"), S, P, RULES) == []


def test_long_excursions_from_next_open_and_zero_exit():
    highs = np.full(N, 100.0)
    lows = np.full(N, 100.0)
    opens = np.full(N, 100.0)
    opens[11:] = 100.0
    opens[16] = 100.5
    highs[13] = 102.0  # favourable 2 before the zero bar
    lows[12] = 99.0  # adverse 1 before the zero bar
    highs[18] = 103.0  # favourable 3 after the zero bar
    lows[20] = 98.0  # adverse 2 after the zero bar
    highs[16] = max(highs[16], 100.5)
    c = Ctx(
        build(
            disp=long_path(),
            opens=opens,
            highs=np.maximum(highs, opens),
            lows=np.minimum(lows, opens),
        ),
        symbol="X",
    )
    df = mm.measure(c, [(10, 1)], RULES, cost_bps={1: 0.0, -1: 0.0})
    r = df.iloc[0]
    assert r.entry == 100.0  # open of bar 11, never the signal-bar close
    assert r.mfe_atr == pytest.approx(3.0)
    assert r.mae_atr == pytest.approx(2.0)
    assert r.reached_zero and r.zero_bar == 15
    assert r.bars_to_zero == 5
    assert r.mfe_pre_atr == pytest.approx(2.0)
    assert r.mae_pre_atr == pytest.approx(1.0)
    assert r.mfe_post_abs_atr == pytest.approx(3.0)
    assert r.mfe_post_incr_atr == pytest.approx(1.0)
    assert r.exit_price == 100.5  # open of bar m+1
    assert r.exit_reason == "ZERO_CROSS"
    assert r.gross_ret == pytest.approx(0.005)
    assert r.zero_cross_price == 100.0  # close of the zero bar
    assert r.bars_to_mfe == 8  # bar 18, counted from the signal bar 10
    assert r.minutes_to_mfe == 40


def test_short_is_mirrored_and_atr_scales():
    disp = np.full(N, 0.6)
    disp[10:] = 0.4
    disp[15:] = -0.1
    lows = np.full(N, 100.0)
    highs = np.full(N, 100.0)
    lows[13] = 98.0  # favourable 2 ATR (ATR = 2)
    highs[12] = 101.0  # adverse 1 price unit = 0.5 ATR
    c = Ctx(build(disp=disp, lows=lows, highs=highs, atr=2.0), symbol="X")
    r = mm.measure(c, [(10, -1)], RULES, cost_bps={1: 0.0, -1: 0.0}).iloc[0]
    assert r.mfe_atr == pytest.approx(1.0)
    assert r.mae_atr == pytest.approx(0.5)
    assert r.mfe_bps == pytest.approx(200.0)
    assert r.mae_bps == pytest.approx(100.0)
    assert r.reached_zero and r.zero_bar == 15


def test_zero_on_the_time_exit_bar_exits_at_its_close():
    disp = long_path(zero=66)  # zero reached on the 14:45 bar itself
    closes = np.full(N, 100.0)
    closes[66] = 101.0
    opens = np.full(N, 100.0)
    c = Ctx(build(disp=disp, closes=closes, highs=np.maximum(closes, opens)), symbol="X")
    r = mm.measure(c, [(10, 1)], RULES, cost_bps={1: 0.0, -1: 0.0}).iloc[0]
    assert r.zero_bar == 66
    assert r.exit_price == 101.0
    assert r.exit_reason == "TIME_EXIT"


def test_never_zero_exits_at_time_exit_close():
    disp = np.full(N, -0.6)
    disp[10:] = -0.4
    closes = np.full(N, 100.0)
    closes[66] = 99.0
    c = Ctx(build(disp=disp, closes=closes, lows=np.minimum(closes, 100.0)), symbol="X")
    r = mm.measure(c, [(10, 1)], RULES, cost_bps={1: 0.0, -1: 0.0}).iloc[0]
    assert not r.reached_zero and r.reached_time_exit_without_zero
    assert r.exit_price == 99.0 and r.exit_reason == "TIME_EXIT"
    assert r.mfe_pre_atr == r.mfe_atr and r.mfe_post_incr_atr == 0.0
    assert np.isnan(r.mfe_post_abs_atr)


def test_cost_is_subtracted_by_direction():
    c = Ctx(build(disp=long_path()), symbol="X")
    r = mm.measure(c, [(10, 1)], RULES, cost_bps={1: 20.0, -1: 30.0}).iloc[0]
    assert r.net_ret == pytest.approx(r.gross_ret - 20.0 / 1e4)


def test_reach_table_counts_adverse_first_on_a_tie():
    highs = np.full(N, 100.0)
    lows = np.full(N, 100.0)
    highs[12] = 101.0
    lows[12] = 99.0  # same bar touches +1 and -1
    highs[14] = 102.0  # +2 later
    c = Ctx(build(disp=long_path(zero=40), highs=highs, lows=lows), symbol="X")
    df = mm.measure(c, [(10, 1)], RULES, cost_bps={1: 0.0, -1: 0.0})
    table = mm.reach_table(df)
    cell = lambda adv, fav: table.loc[adv, fav]  # noqa: E731
    assert cell(1.0, 1.0) == 0.0  # tie in one bar -> adverse first
    assert cell(1.0, 2.0) == 0.0  # +2 comes only after -1 was hit
    assert cell(1.25, 1.0) == 1.0  # -1.25 is never touched, +1 is
    assert cell(1.5, 2.0) == 1.0


def test_engine_traded_flag_and_engine_agreement():
    rng = np.random.default_rng(7)
    days = tuple(f"2026-06-{d:02d}" for d in range(1, 9))
    n = N * len(days)
    price = 100 + np.cumsum(rng.normal(0, 0.25, n))
    op = np.concatenate([[100.0], price[:-1]])
    raw = pd.DataFrame(
        {
            "Open": op,
            "High": np.maximum(op, price) + 0.1,
            "Low": np.minimum(op, price) - 0.1,
            "Close": price,
            "Volume": np.full(n, 1000.0),
        },
        index=build(days=days).index,
    )
    prepared = S.prepare(raw, P)
    run = RunConfig().with_(**{**S.run_overrides, "warmup_bars": 20})
    c = Ctx(prepared, symbol="X")
    trades = simulate(c, S, P, run)
    assert trades, "synthetic path should produce at least one engine trade"
    rules = mm.WindowRules.from_run(run, 5)
    picks = mm.find_signals(c, S, P, rules)
    df = mm.measure(c, picks, rules, cost_bps={1: 0.0, -1: 0.0})
    mm.mark_engine_traded(df, "X", trades)
    assert df.engine_traded.sum() == len(trades)
    for t in trades:
        row = df[(df.entry_time == t.entry_time)].iloc[0]
        assert row.entry == pytest.approx(t.entry)
        assert row.exit_price == pytest.approx(t.exit)
        assert row.direction == t.direction
        assert row.exit_reason == t.reason


def test_placebo_is_seeded_matched_and_same_day():
    two = build(days=("2026-06-01", "2026-06-02"))
    disp = np.full(2 * N, -0.3)
    disp[N + 5 : N + 10] = -0.6
    disp[N + 10] = -0.4
    two["disp"] = disp
    two["disp_prev"] = (
        pd.Series(disp).shift(1).where(two["day"].eq(two["day"].shift(1)).to_numpy()).to_numpy()
    )
    c = Ctx(two, symbol="X")
    real = mm.find_signals(c, S, P, RULES)
    assert real == [(N + 10, 1)]
    a = mm.placebo_picks(c, real, RULES, seed=5, mode="day")
    assert a == mm.placebo_picks(c, real, RULES, seed=5, mode="day")
    assert len(a) == len(real)
    assert all(c["day"][i] == c["day"][N + 10] for i, _ in a)
    assert all(i != N + 10 and d == 1 for i, d in a)  # never the real signal bar, same direction
    assert any(mm.placebo_picks(c, real, RULES, seed=s, mode="day") != a for s in range(6, 16))
    mask = mm.executable_mask(c, RULES)
    assert all(mask[i] for i, _ in a)
    for i, d in mm.placebo_picks(c, real, RULES, seed=5, mode="zone"):
        assert (-0.5 < c["disp"][i] < 0.0) if d == 1 else (0.0 < c["disp"][i] < 0.5)


def test_measure_without_levels_drops_reach_columns():
    c = Ctx(build(disp=long_path()), symbol="X")
    df = mm.measure(c, [(10, 1)], RULES, cost_bps={1: 0.0, -1: 0.0}, levels=False)
    assert "adv_0.5" not in df.columns and "mfe_atr" in df.columns


def test_entry_on_the_time_exit_bar_exits_at_its_close():
    # cutoff 14:45: the 14:40 bar (j=65) signals, fills at the 14:45 open (j=66), exits at its close
    rules = mm.WindowRules(0, 14 * 60 + 45, 0, 14 * 60 + 45)
    disp = np.full(N, -0.6)
    disp[65] = -0.4
    closes = np.full(N, 100.0)
    closes[66] = 100.7
    c = Ctx(build(disp=disp, closes=closes, highs=np.maximum(closes, 100.0)), symbol="X")
    assert mm.find_signals(c, S, P, rules) == [(65, 1)]
    r = mm.measure(c, [(65, 1)], rules, cost_bps={1: 0.0, -1: 0.0}).iloc[0]
    assert r.window_end == 66 and r.exit_price == 100.7 and r.exit_reason == "TIME_EXIT"


def test_summary_stats_shape():
    s = mm.dist(pd.Series([1.0, 2.0, 3.0, 4.0, np.nan]))
    assert s["n"] == 4 and s["mean"] == 2.5 and s["median"] == 2.5
    assert set(s) == {"n", "mean", "median", "p10", "p25", "p75", "p90"}
    assert mm.dist(pd.Series([], dtype=float))["n"] == 0
