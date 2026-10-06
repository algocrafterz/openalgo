"""Session VWAP standard deviation, the fixed 15-minute block series and its causal mapping.

Hand-computed numbers pin each formula; the boundary tests pin which 15-minute bar a 5-minute
decision may read (a 15-minute bar is usable only once all three of its 5-minute bars closed).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import indicators as bt_ind
from signal_engine.features.htf import completed_htf_position, resample_15m
from signal_engine.features.price import adx, session_vwap_sd

IST = "Asia/Kolkata"


def session(day: str, rows, start="09:15"):
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="5min", tz=IST)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def full_session(day: str, seed: int, n: int = 72):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.05, 0.4, len(c))
    lo = np.minimum(o, c) - rng.uniform(0.05, 0.4, len(c))
    v = rng.integers(100, 5000, len(c)).astype(float)
    return session(day, list(zip(o, h, lo, c, v, strict=True)))


class TestSessionVwapSd:
    def test_hand_computed_three_bars(self):
        # tp = 10, 12, 10 with volumes 100, 300, 100
        df = session(
            "2026-06-01", [(10, 12, 8, 10, 100), (11, 14, 10, 12, 300), (10, 12, 9, 9, 100)]
        )
        vwap, sd = session_vwap_sd(df)
        assert vwap.tolist() == pytest.approx([10.0, 11.5, 11.2])
        # bar 2: 53200/400 - 11.5^2 = 0.75; bar 3: 63200/500 - 11.2^2 = 0.96
        assert sd.tolist() == pytest.approx([0.0, math.sqrt(0.75), math.sqrt(0.96)])

    def test_resets_every_session(self):
        a = session("2026-06-01", [(10, 12, 8, 10, 100), (11, 14, 10, 12, 300)])
        b = session("2026-06-02", [(50, 52, 48, 50, 10)])
        vwap, sd = session_vwap_sd(pd.concat([a, b]))
        assert vwap.iloc[-1] == pytest.approx(50.0)
        assert sd.iloc[-1] == pytest.approx(0.0)

    def test_variance_floor_is_zero_not_nan(self):
        # identical typical prices: the float difference may be a tiny negative number
        df = session("2026-06-01", [(0.1, 0.3, 0.1, 0.2, 7)] * 5)
        _, sd = session_vwap_sd(df)
        assert (sd == 0.0).all()

    def test_zero_volume_start_is_nan(self):
        df = session("2026-06-01", [(10, 12, 8, 10, 0), (11, 14, 10, 12, 300)])
        vwap, sd = session_vwap_sd(df)
        assert np.isnan(vwap.iloc[0]) and np.isnan(sd.iloc[0])
        assert vwap.iloc[1] == pytest.approx(12.0)


class TestAdx:
    def test_matches_the_backtest_indicator(self):
        df = pd.concat([full_session("2026-06-01", 1), full_session("2026-06-02", 2)])
        pd.testing.assert_series_equal(adx(df, 14), bt_ind.adx(df, 14))


class TestResample15m:
    def test_blocks_of_three_from_0915(self):
        df = full_session("2026-06-01", 3)
        m = resample_15m(df)
        assert len(m) == 24
        assert m.index[0] == pd.Timestamp("2026-06-01 09:15", tz=IST)
        assert m.index[-1] == pd.Timestamp("2026-06-01 15:00", tz=IST)
        first = df.iloc[:3]
        assert m["Open"].iloc[0] == first["Open"].iloc[0]
        assert m["High"].iloc[0] == first["High"].max()
        assert m["Low"].iloc[0] == first["Low"].min()
        assert m["Close"].iloc[0] == first["Close"].iloc[-1]
        assert m["Volume"].iloc[0] == first["Volume"].sum()

    def test_closing_auction_bars_never_enter(self):
        base = full_session("2026-06-01", 4)
        auction = session("2026-06-01", [(99, 130, 70, 120, 9e6)] * 3, start="15:15")
        with_auction = pd.concat([base, auction])
        assert with_auction.index[-1].strftime("%H:%M") == "15:25"
        pd.testing.assert_frame_equal(resample_15m(base), resample_15m(with_auction))

    def test_incomplete_block_contributes_nothing(self):
        df = full_session("2026-06-01", 5).drop(pd.Timestamp("2026-06-01 09:35", tz=IST))
        m = resample_15m(df)
        assert len(m) == 23
        assert pd.Timestamp("2026-06-01 09:30", tz=IST) not in m.index

    def test_missing_session_adds_no_block(self):
        df = pd.concat([full_session("2026-06-01", 6), full_session("2026-06-03", 7)])
        assert len(resample_15m(df)) == 48


class TestCompletedMapping:
    def setup_method(self):
        self.df = pd.concat([full_session("2026-06-01", 8), full_session("2026-06-02", 9)])
        self.m = resample_15m(self.df)
        self.pos = completed_htf_position(self.df.index, self.m.index)

    def at(self, stamp):
        return self.pos[self.df.index.get_loc(pd.Timestamp(stamp, tz=IST))]

    def test_0925_bar_sees_the_0915_block(self):
        # the 09:25 bar closes at 09:30, exactly when the 09:15-09:30 block completes
        assert self.m.index[self.at("2026-06-02 09:25")] == pd.Timestamp("2026-06-02 09:15", tz=IST)

    def test_0920_bar_sees_only_the_previous_session(self):
        got = self.m.index[self.at("2026-06-02 09:20")]
        assert got == pd.Timestamp("2026-06-01 15:00", tz=IST)

    def test_first_bars_have_no_completed_block(self):
        assert self.at("2026-06-01 09:15") == -1
        assert self.at("2026-06-01 09:20") == -1
        assert self.at("2026-06-01 09:25") == 0

    def test_never_reads_a_block_closing_after_the_bar(self):
        close_5m = self.df.index + pd.Timedelta(minutes=5)
        ok = self.pos >= 0
        close_15m = self.m.index[self.pos[ok]] + pd.Timedelta(minutes=15)
        assert (close_15m <= close_5m[ok]).all()
        # and it is the LATEST such block
        nxt = self.pos[ok] + 1
        has_next = nxt < len(self.m)
        later = self.m.index[nxt[has_next]] + pd.Timedelta(minutes=15)
        assert (later > close_5m[ok][has_next]).all()
