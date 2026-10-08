"""IDEA-032 adapter: displacement = (EMA9(close) - session VWAP(hlc3)) / ATR14.

LONG  : previous displacement <= -0.50 and current displacement > -0.50
SHORT : previous displacement >= +0.50 and current displacement < +0.50
EXIT  : displacement reaches 0 (long: >= 0, short: <= 0), judged on the completed bar.
The +/-0.35 WATCH level and the zero cross never open a trade.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.ema_vwap_atr_displacement import (
    EmaVwapAtrDisplacement,
    EmaVwapAtrDisplacementParams,
)
from signal_engine.backtest.types import Ctx, Position

IST = "Asia/Kolkata"
P = EmaVwapAtrDisplacementParams()
S = EmaVwapAtrDisplacement()


def frame(n_days=3, per_day=30, seed=1):
    rng = np.random.default_rng(seed)
    idx, rows = [], []
    price = 100.0
    for d in range(n_days):
        day = pd.Timestamp("2026-06-01", tz=IST) + pd.Timedelta(days=d)
        for k in range(per_day):
            idx.append(day + pd.Timedelta(hours=9, minutes=15 + 5 * k))
            op = price
            price = price + rng.normal(0, 0.4)
            rows.append([op, max(op, price) + 0.2, min(op, price) - 0.2, price, 1000 + 10 * k])
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex(idx))


def ctx_with_disp(prev, cur):
    """Two-bar Ctx whose displacement columns are set directly, to test the rules alone."""
    df = pd.DataFrame({"Open": [100.0, 100.0], "High": [101.0, 101.0], "Low": [99.0, 99.0],
                       "Close": [100.0, 100.0], "Volume": [1.0, 1.0],
                       "disp": [prev, cur], "disp_prev": [np.nan, prev], "atr": [1.0, 1.0]})
    return Ctx(df, "T")


class TestDisplacementFormula:
    def test_matches_the_stated_definition(self):
        raw = frame()
        d = S.prepare(raw.copy(), P)
        day = pd.Series(raw.index.date, index=raw.index)
        ema9 = ind.ema(raw["Close"], 9)
        hlc3 = (raw["High"] + raw["Low"] + raw["Close"]) / 3
        vwap = (hlc3 * raw["Volume"]).groupby(day).cumsum() / raw["Volume"].groupby(day).cumsum()
        atr14 = ind.rma(ind.true_range(raw), 14)
        expected = (ema9 - vwap) / atr14
        np.testing.assert_allclose(d["disp"].to_numpy(), expected.to_numpy(), rtol=1e-12)

    def test_vwap_resets_each_session_but_ema_and_atr_carry(self):
        raw = frame()
        d = S.prepare(raw.copy(), P)
        first_of_day2 = 30
        assert d["vwap"].iloc[first_of_day2] == pytest.approx(
            (raw["High"].iloc[first_of_day2] + raw["Low"].iloc[first_of_day2]
             + raw["Close"].iloc[first_of_day2]) / 3)
        # ATR14 on day 2 bar 0 still contains day 1: it is not the first-bar warm-up value
        assert d["atr"].iloc[first_of_day2] != pytest.approx(
            raw["High"].iloc[first_of_day2] - raw["Low"].iloc[first_of_day2])

    def test_first_bar_of_a_session_has_no_previous_displacement(self):
        d = S.prepare(frame(), P)
        for first in (0, 30, 60):
            assert np.isnan(d["disp_prev"].iloc[first])
        assert d["disp_prev"].iloc[31] == pytest.approx(d["disp"].iloc[30])

    def test_no_lookahead_in_prepare(self):
        raw = frame()
        full = S.prepare(raw.copy(), P)
        part = S.prepare(raw.iloc[:50].copy(), P)
        np.testing.assert_allclose(full["disp"].iloc[:50].to_numpy(), part["disp"].to_numpy())

    def test_zero_atr_gives_no_displacement(self):
        raw = frame(n_days=1, per_day=20)
        raw[["Open", "High", "Low", "Close"]] = 100.0
        d = S.prepare(raw.copy(), P)
        assert d["disp"].isna().all()


class TestEntryRules:
    @pytest.mark.parametrize("prev,cur,fires", [
        (-0.50, -0.49, True),    # previous exactly at the threshold counts
        (-0.80, -0.49, True),
        (-0.51, -0.50, False),   # current exactly at the threshold does not exceed it
        (-0.49, -0.40, False),   # was already above the threshold
        (-0.60, -0.55, False),   # still below
        (-0.36, -0.34, False),   # the WATCH level never trades
        (-0.10, 0.10, False),    # the zero cross is not an entry
    ])
    def test_long(self, prev, cur, fires):
        sig = S.entry(ctx_with_disp(prev, cur), 1, P, 1)
        assert (sig is not None) is fires

    @pytest.mark.parametrize("prev,cur,fires", [
        (0.50, 0.49, True),
        (0.80, 0.49, True),
        (0.51, 0.50, False),
        (0.49, 0.40, False),
        (0.60, 0.55, False),
        (0.36, 0.34, False),
        (0.10, -0.10, False),
    ])
    def test_short(self, prev, cur, fires):
        sig = S.entry(ctx_with_disp(prev, cur), 1, P, -1)
        assert (sig is not None) is fires

    def test_long_and_short_are_mirror_images(self):
        for prev, cur in [(-0.6, -0.4), (-0.5, -0.5), (-0.3, -0.2)]:
            lng = S.entry(ctx_with_disp(prev, cur), 1, P, 1) is not None
            sht = S.entry(ctx_with_disp(-prev, -cur), 1, P, -1) is not None
            assert lng == sht

    def test_other_direction_does_not_fire(self):
        assert S.entry(ctx_with_disp(-0.6, -0.4), 1, P, -1) is None
        assert S.entry(ctx_with_disp(0.6, 0.4), 1, P, 1) is None

    def test_signal_has_no_stop_and_no_target(self):
        sig = S.entry(ctx_with_disp(-0.6, -0.4), 1, P, 1)
        assert sig.sl is None
        assert np.isnan(sig.tp)

    def test_nan_displacement_never_fires(self):
        assert S.entry(ctx_with_disp(np.nan, -0.4), 1, P, 1) is None
        assert S.entry(ctx_with_disp(-0.6, np.nan), 1, P, 1) is None

    def test_a_signal_needs_a_previous_bar_of_the_same_session(self):
        c = ctx_with_disp(-0.6, -0.4)
        c.a["disp_prev"][1] = np.nan  # session start: previous bar belongs to another session
        assert S.entry(c, 1, P, 1) is None

    def test_adapter_registers_the_engine_settings_it_needs(self):
        ro = S.run_overrides
        assert ro["custom_exit_fill"] == "next_open"
        assert ro["skip_open_minutes"] == 0
        assert ro["entry_cutoff_min"] == 14 * 60 + 45  # equals the profile standing time exit


class TestExitRule:
    def pos(self, direction):
        return Position(direction=direction, entry=100.0, signal_price=100.0, sl=float("nan"),
                        sl_eff=float("nan"), tp=float("nan"), risk=float("nan"), tag="T",
                        entry_bar=0)

    @pytest.mark.parametrize("cur,exits", [(-0.01, False), (0.0, True), (0.3, True)])
    def test_long_exits_when_displacement_reaches_zero(self, cur, exits):
        r = S.custom_exit(ctx_with_disp(-0.4, cur), 1, P, self.pos(1))
        assert (r is not None) is exits

    @pytest.mark.parametrize("cur,exits", [(0.01, False), (0.0, True), (-0.3, True)])
    def test_short_exits_when_displacement_reaches_zero(self, cur, exits):
        r = S.custom_exit(ctx_with_disp(0.4, cur), 1, P, self.pos(-1))
        assert (r is not None) is exits

    def test_nan_displacement_does_not_exit(self):
        assert S.custom_exit(ctx_with_disp(-0.4, np.nan), 1, P, self.pos(1)) is None
