"""Intraday random-entry baseline: same symbol, same hold in bars, same session."""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import baseline
from signal_engine.backtest.types import Trade


def _frame(days=30, bars=20, drift=0.0, seed=0):
    rng = np.random.default_rng(seed)
    idx = []
    for d in pd.bdate_range("2026-01-05", periods=days):
        idx += [d + pd.Timedelta(hours=9, minutes=15 + 5 * i) for i in range(bars)]
    index = pd.DatetimeIndex(idx)
    step = rng.normal(drift, 0.001, len(index))
    close = 100.0 * np.exp(np.cumsum(step))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "Close": close}, index=index)


def _trade(frame, bar, hold, symbol="A.NS", r_net=0.2, direction=1):
    t0, t1 = frame.index[bar], frame.index[bar + hold]
    return Trade(
        symbol=symbol,
        day=str(t0.date()),
        direction=direction,
        tag="t",
        entry_time=t0,
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=t1,
        exit=101.0,
        r_gross=r_net,
        r_net=r_net,
    )


class TestIntradayBaseline:
    def test_returns_the_same_shape_as_the_swing_baseline(self):
        f = _frame()
        trades = [_trade(f, 25 + 20 * i, 6) for i in range(10)]
        out = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=10)
        for key in ("method", "trades", "gross_bps", "net_bps", "excess_net_bps", "trade_net_bps"):
            assert key in out
        assert out["trades"] == 10

    def test_random_entries_never_cross_a_session(self):
        f = _frame(days=5, bars=10)
        # drift huge only at the overnight gap: a cross-session draw would show it
        closes = f["Close"].to_numpy().copy()
        for k in range(1, 5):
            closes[k * 10 :] *= 2.0
        opens = np.r_[closes[0], closes[:-1]]
        for k in range(1, 5):
            opens[k * 10] = closes[k * 10]  # the gap belongs between sessions, not inside one
        f = f.assign(Close=closes, Open=opens)
        trades = [_trade(f, 12, 4)]
        out = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=200)
        assert abs(out["gross_bps"]) < 500  # an overnight doubling would be ~10000 bps

    def test_deterministic_for_a_seed(self):
        f = _frame()
        trades = [_trade(f, 25 + 20 * i, 6) for i in range(8)]
        a = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=10, seed=3)
        b = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=10, seed=3)
        assert a == b

    def test_samples_when_there_are_more_trades_than_the_cap(self):
        f = _frame(days=60)
        trades = [_trade(f, 25 + 20 * i, 6) for i in range(50)]
        out = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=5, max_trades=20)
        assert out["trades"] == 20
        assert out["population_trades"] == 50
        assert out["sampled"] is True

    def test_no_sampling_flag_when_under_the_cap(self):
        f = _frame()
        trades = [_trade(f, 25 + 20 * i, 6) for i in range(5)]
        out = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=5)
        assert out["sampled"] is False

    def test_trade_without_a_frame_is_skipped_and_counted(self):
        f = _frame()
        trades = [_trade(f, 25, 6), _trade(f, 45, 6, symbol="MISSING.NS")]
        out = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=5)
        assert out["skipped"] == 1

    def test_no_matchable_trade_returns_none(self):
        f = _frame()
        assert baseline.random_entry_baseline_intraday([], {"A.NS": f}) is None

    def test_edge_beats_the_random_baseline_on_a_trending_book(self):
        f = _frame(days=60, drift=0.0)
        trades = [_trade(f, 25 + 20 * i, 6, r_net=0.5) for i in range(40)]
        out = baseline.random_entry_baseline_intraday(trades, {"A.NS": f}, draws=20)
        assert out["excess_net_bps"] > 0

    def test_short_trades_are_compared_short(self):
        f = _frame(days=40, drift=0.0008, seed=4)
        long_t = [_trade(f, 25 + 20 * i, 6, direction=1) for i in range(20)]
        short_t = [_trade(f, 25 + 20 * i, 6, direction=-1) for i in range(20)]
        lo = baseline.random_entry_baseline_intraday(long_t, {"A.NS": f}, draws=30)
        sh = baseline.random_entry_baseline_intraday(short_t, {"A.NS": f}, draws=30)
        assert lo["gross_bps"] == pytest.approx(-sh["gross_bps"], rel=1e-6)


class TestSampling:
    def test_sample_trades_is_deterministic_and_bounded(self):
        f = _frame(days=60)
        trades = [_trade(f, 25 + 20 * i, 6) for i in range(50)]
        a = baseline.sample_trades(trades, 20, seed=1)
        b = baseline.sample_trades(trades, 20, seed=1)
        assert len(a) == 20 and a == b

    def test_sample_trades_returns_everything_under_the_cap(self):
        f = _frame(days=60)
        trades = [_trade(f, 25 + 20 * i, 6) for i in range(5)]
        assert len(baseline.sample_trades(trades, 20)) == 5

    def test_caller_supplied_population_is_reported(self):
        f = _frame(days=60)
        trades = [_trade(f, 25 + 20 * i, 6) for i in range(10)]
        out = baseline.random_entry_baseline_intraday(
            trades, {"A.NS": f}, draws=5, population_trades=500
        )
        assert out["sampled"] is True and out["population_trades"] == 500
