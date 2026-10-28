"""IDEA-034 balance breakout: value area, balance detection, breakout trigger, no look-ahead."""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.balance_breakout import (
    BalanceBreakout,
    BalanceParams,
    value_area,
)
from signal_engine.backtest.types import Ctx, RunConfig

IST = "Asia/Kolkata"
N_HIST = 26


def _day(date, wave=1.5, base=100.0, vol=1000.0):
    """One session of 72 five-minute bars (09:15 .. 15:10) oscillating around `base`."""
    idx = pd.date_range(f"{date} 09:15", periods=72, freq="5min", tz=IST)
    c = base + wave * np.sin(np.linspace(0, 6 * np.pi, 72))
    return pd.DataFrame(
        {"Open": c, "High": c + 0.3, "Low": c - 0.3, "Close": c, "Volume": vol}, index=idx
    )


def _frame(waves=None, last_wave=0.0):
    days = pd.bdate_range("2026-01-05", periods=N_HIST + 1)
    parts = []
    for k, dt in enumerate(days[:-1]):
        parts.append(_day(dt.date(), wave=(waves[k] if waves else 1.5)))
    parts.append(_day(days[-1].date(), wave=last_wave))  # trade day: flat at 100
    return pd.concat(parts), days[-1].date()


def _set(df, date, hhmm, **cols):
    ts = pd.Timestamp(f"{date} {hhmm}", tz=IST)
    for k, v in cols.items():
        df.loc[ts, k] = v
    return df


def _push(df, date, hhmm, price):
    return _set(df, date, hhmm, Open=price, High=price + 0.2, Low=price - 0.2, Close=price)


def _prep(df, **kw):
    p = BalanceParams(**kw)
    return BalanceBreakout().prepare_for("T", df, p), p


def _sigs(d, date):
    s = d[d["day"] == date]
    return s[s["sig_dir"] != 0]


def test_value_area_concentrated_volume_is_narrow_and_contains_poc():
    high = np.array([100.5, 101.0, 100.4, 105.0])
    low = np.array([99.5, 99.8, 99.6, 104.0])
    vol = np.array([1000.0, 5000.0, 1000.0, 10.0])
    val, vah, poc = value_area(high, low, vol, 100.5)
    assert val <= poc <= vah
    assert vah < 102  # the thin 104-105 print is outside the 70 percent area
    assert val > 99


def test_value_area_holds_at_least_70_percent_and_none_without_volume():
    rng = np.random.default_rng(1)
    c = 100 + rng.normal(0, 1, 60).cumsum() * 0.1
    high, low, vol = c + 0.2, c - 0.2, rng.uniform(100, 200, 60)
    val, vah, _ = value_area(high, low, vol, float(c.mean()))
    inside = 0.0
    for h, lo, v in zip(high, low, vol, strict=True):
        overlap = max(0.0, min(h, vah) - max(lo, val))
        inside += v * overlap / (h - lo)
    assert inside / vol.sum() >= 0.69
    assert value_area(high, low, np.zeros(60), 100.0) is None


def test_long_breakout_fresh_cross_signal_levels():
    df, day = _frame()
    _push(df, day, "10:10", 110.0)
    d, p = _prep(df)
    s = _sigs(d, day)
    assert len(s) == 1 and s.index[0] == pd.Timestamp(f"{day} 10:10", tz=IST)
    row = s.iloc[0]
    assert row["sig_dir"] == 1
    assert row["sig_sl"] == pytest.approx(row["bal_hi"] - 0.25 * row["atr_d"])
    assert row["sig_tp"] == pytest.approx(row["bal_hi"] + row["bal_h"])
    assert row["h_atr"] <= p.atr_max_multiple and row["ov_min"] >= p.min_va_overlap


def test_short_breakout_mirrors():
    df, day = _frame()
    _push(df, day, "10:10", 90.0)
    d, _ = _prep(df)
    row = _sigs(d, day).iloc[0]
    assert row["sig_dir"] == -1
    assert row["sig_sl"] == pytest.approx(row["bal_lo"] + 0.25 * row["atr_d"])
    assert row["sig_tp"] == pytest.approx(row["bal_lo"] - row["bal_h"])


def test_gap_away_without_fresh_cross_is_not_chased():
    df, day = _frame()
    _push(df, day, "09:40", 110.0)  # 09:45 period close already outside balance
    _push(df, day, "10:10", 110.0)
    d, _ = _prep(df)
    assert _sigs(d, day).empty


def test_window_last_period_in_and_after_13_out():
    df, day = _frame()
    _push(df, day, "12:40", 110.0)  # period closing 12:45 is the last allowed
    d, _ = _prep(df)
    assert len(_sigs(d, day)) == 1
    df2, day = _frame()
    _push(df2, day, "13:10", 110.0)  # period closing 13:15 is too late
    d2, _ = _prep(df2)
    assert _sigs(d2, day).empty


def test_intra_period_close_is_not_a_30_minute_close():
    df, day = _frame()
    _push(df, day, "10:05", 110.0)  # mid-period only; 10:10 bar closes back inside
    d, _ = _prep(df)
    assert _sigs(d, day).empty


def test_only_first_breakout_of_day_is_signalled():
    df, day = _frame()
    _push(df, day, "10:10", 110.0)
    _push(df, day, "10:40", 90.0)
    d, _ = _prep(df)
    s = _sigs(d, day)
    assert len(s) == 1 and s.iloc[0]["sig_dir"] == 1


def test_wide_range_history_is_not_balance_but_non_balance_placebo_fires():
    waves = [1.5 + 3.0 * (k % 2) for k in range(N_HIST)]  # alternating wide days
    waves = [w if k % 2 == 0 else 8.0 for k, w in enumerate(waves)]
    base = [100.0 + 6.0 * (k % 2) for k in range(N_HIST)]
    days = pd.bdate_range("2026-01-05", periods=N_HIST + 1)
    parts = [_day(dt.date(), wave=waves[k], base=base[k]) for k, dt in enumerate(days[:-1])]
    parts.append(_day(days[-1].date(), wave=0.0, base=103.0))
    df, day = pd.concat(parts), days[-1].date()
    d, p = _prep(df, atr_max_multiple=0.5)
    row = d[d["day"] == day].iloc[0]
    assert not (row["h_atr"] <= p.atr_max_multiple)
    _push(df, day, "10:10", 130.0)
    d, _ = _prep(df, atr_max_multiple=0.5)
    assert _sigs(d, day).empty
    dp, _ = _prep(df, atr_max_multiple=0.5, placebo="non_balance")
    assert len(_sigs(dp, day)) == 1


def test_two5_trigger_needs_two_closes_and_a_fresh_cross():
    df, day = _frame()
    _push(df, day, "10:15", 110.0)
    d, _ = _prep(df, trigger="two5")
    assert _sigs(d, day).empty  # a single close beyond is not acceptance
    _push(df, day, "10:20", 110.0)
    d, _ = _prep(df, trigger="two5")
    s = _sigs(d, day)
    assert len(s) == 1 and s.index[0] == pd.Timestamp(f"{day} 10:20", tz=IST)


def test_mid_stop_uses_composite_midpoint():
    df, day = _frame()
    _push(df, day, "10:10", 110.0)
    d, _ = _prep(df, stop_mode="mid")
    row = _sigs(d, day).iloc[0]
    assert row["sig_sl"] == pytest.approx((row["bal_hi"] + row["bal_lo"]) / 2)


def test_random_entry_placebo_is_seeded_and_one_per_balance_day():
    df, day = _frame()
    a, _ = _prep(df, placebo="random_entry")
    b, _ = _prep(df, placebo="random_entry")
    assert (a["sig_dir"].to_numpy() == b["sig_dir"].to_numpy()).all()
    assert len(_sigs(a, day)) == 1


def test_no_lookahead_future_bars_and_today_volume_do_not_change_the_signal():
    df, day = _frame()
    _push(df, day, "10:10", 110.0)
    base, _ = _prep(df)
    alt = df.copy()
    late = alt.index >= pd.Timestamp(f"{day} 10:15", tz=IST)
    alt.loc[late, ["Open", "High", "Low", "Close"]] = 50.0  # rewrite the rest of the day
    alt.loc[alt.index.date == day, "Volume"] = 1e9  # and today's volume
    changed, _ = _prep(alt)
    ts = pd.Timestamp(f"{day} 10:10", tz=IST)
    for col in ("sig_dir", "sig_sl", "sig_tp", "bal_hi", "bal_lo", "atr_d"):
        assert changed.loc[ts, col] == pytest.approx(base.loc[ts, col]), col


def test_balance_uses_only_completed_prior_sessions():
    df, day = _frame()
    base, _ = _prep(df)
    alt = df.copy()
    alt.loc[alt.index.date == day, ["High", "Low", "Open", "Close"]] = 500.0
    changed, _ = _prep(alt)
    r0 = base[base["day"] == day].iloc[0]
    r1 = changed[changed["day"] == day].iloc[0]
    assert r0["bal_hi"] == pytest.approx(r1["bal_hi"])
    assert r0["bal_lo"] == pytest.approx(r1["bal_lo"])


def test_engine_enters_at_next_open_and_frozen_params_bind():
    df, day = _frame()
    _push(df, day, "10:10", 102.0)
    _push(df, day, "10:15", 102.2)
    strat, p = BalanceBreakout(), BalanceParams()
    ctx = Ctx(strat.prepare_for("T", df, p), symbol="T")
    run = RunConfig().with_(**BalanceBreakout.run_overrides)
    trades = [t for t in simulate(ctx, strat, p, run) if t.day == day]
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == 1 and t.entry_time == pd.Timestamp(f"{day} 10:15", tz=IST)
    assert {f.name for f in dataclasses.fields(p)} >= {"balance_days", "atr_max_multiple"}


def _height_pct(row, side):
    edge = row["bal_hi"] if side == 1 else row["bal_lo"]
    return row["bal_h"] / edge * 100.0


def test_min_height_pct_rejects_a_balance_narrower_than_the_threshold():
    df, day = _frame()
    _push(df, day, "10:10", 110.0)
    d, _ = _prep(df)
    h_pct = _height_pct(_sigs(d, day).iloc[0], 1)
    d2, _ = _prep(df, min_height_pct=h_pct + 0.01)
    assert _sigs(d2, day).empty


def test_min_height_pct_accepts_a_wide_enough_balance_on_both_sides():
    df, day = _frame()
    _push(df, day, "10:10", 110.0)
    d, _ = _prep(df)
    h_pct = _height_pct(_sigs(d, day).iloc[0], 1)
    d2, _ = _prep(df, min_height_pct=h_pct - 0.01)
    assert len(_sigs(d2, day)) == 1
    dfs, day = _frame()
    _push(dfs, day, "10:10", 90.0)
    ds, _ = _prep(dfs)
    hs = _height_pct(_sigs(ds, day).iloc[0], -1)  # short measured against the composite low
    assert len(_sigs(_prep(dfs, min_height_pct=hs - 0.01)[0], day)) == 1
    assert _sigs(_prep(dfs, min_height_pct=hs + 0.01)[0], day).empty


def test_min_height_pct_zero_reproduces_v1_signals():
    df, day = _frame()
    _push(df, day, "10:10", 110.0)
    _push(df, day, "11:10", 90.0)
    assert BalanceParams().min_height_pct == 1.5  # v2 primary is the shipped default
    d0, _ = _prep(df, min_height_pct=0.0)
    s = _sigs(d0, day)
    assert len(s) == 1 and s.iloc[0]["sig_dir"] == 1  # the v1 first-breakout rule is untouched
    tiny, _ = _prep(df, min_height_pct=1e-9)
    cols = ["sig_dir", "sig_sl", "sig_tp"]
    pd.testing.assert_frame_equal(d0[cols], tiny[cols])


def test_min_height_pct_also_filters_the_non_balance_placebo():
    waves = [1.5 if k % 2 == 0 else 8.0 for k in range(N_HIST)]
    base = [100.0 + 6.0 * (k % 2) for k in range(N_HIST)]
    days = pd.bdate_range("2026-01-05", periods=N_HIST + 1)
    parts = [_day(dt.date(), wave=waves[k], base=base[k]) for k, dt in enumerate(days[:-1])]
    parts.append(_day(days[-1].date(), wave=0.0, base=103.0))
    df, day = pd.concat(parts), days[-1].date()
    _push(df, day, "10:10", 130.0)
    dp, _ = _prep(df, atr_max_multiple=0.5, placebo="non_balance")
    h_pct = _height_pct(_sigs(dp, day).iloc[0], 1)
    dq, _ = _prep(df, atr_max_multiple=0.5, placebo="non_balance", min_height_pct=h_pct + 0.01)
    assert _sigs(dq, day).empty
