"""The input builder composes the reusable features into the BREAKOUT core's input frame."""

import numpy as np
import pandas as pd

from signal_engine.features.levels import adr_prev, prev_day_hl
from signal_engine.features.price import atr, session_vwap
from signal_engine.features.value_area import prev_session_value_area
from signal_engine.strategy_core.breakout import compute_signals
from signal_engine.strategy_core.inputs import INPUT_COLUMNS, build_breakout_inputs

IST = "Asia/Kolkata"
DAYS = ("2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29")


def synth(minutes=75, seed=3):
    rng = np.random.default_rng(seed)
    m1_parts, m5_parts = [], []
    for d in DAYS:
        idx = pd.date_range(f"{d} 09:15", periods=minutes, freq="1min", tz=IST)
        walk = 100 + np.cumsum(rng.normal(0, 0.1, minutes))
        noise = rng.normal(0, 0.05, minutes)
        m1 = pd.DataFrame(
            {
                "Open": walk,
                "Close": walk + noise,
                "High": np.maximum(walk, walk + noise) + np.abs(rng.normal(0, 0.03, minutes)),
                "Low": np.minimum(walk, walk + noise) - np.abs(rng.normal(0, 0.03, minutes)),
                "Volume": rng.integers(100, 1000, minutes).astype(float),
            },
            index=idx,
        )
        m5 = m1.resample("5min", label="left", closed="left", origin=idx[0]).agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        )
        m1_parts.append(m1)
        m5_parts.append(m5)
    return pd.concat(m1_parts), pd.concat(m5_parts)


def build(m1, m5, **kw):
    return build_breakout_inputs(m5, m1, tick=0.05, adr_days=2, **kw)


class TestBuildBreakoutInputs:
    def test_has_exactly_the_columns_the_core_reads_and_the_bar_index(self):
        m1, m5 = synth()
        out = build(m1, m5)
        assert list(out.columns) == list(INPUT_COLUMNS)
        assert out.index.equals(m5.index)

    def test_columns_are_the_shared_features_not_reimplementations(self):
        m1, m5 = synth()
        out = build(m1, m5)
        pd.testing.assert_series_equal(out["atr"], atr(m5, 14), check_names=False)
        pd.testing.assert_series_equal(out["vwap"], session_vwap(m5), check_names=False)
        pdh, pdl = prev_day_hl(m5)
        pd.testing.assert_series_equal(out["pdh"], pdh, check_names=False)
        pd.testing.assert_series_equal(out["pdl"], pdl, check_names=False)
        pd.testing.assert_series_equal(out["adr"], adr_prev(m5, 2), check_names=False)
        va = prev_session_value_area(m1, m5.index, tick=0.05)
        pd.testing.assert_series_equal(out["ppoc"], va["ppoc"], check_names=False)

    def test_rvol_is_zero_where_the_baseline_is_unknown(self):
        m1, m5 = synth()
        out = build(m1, m5)
        assert (out["rvol"] >= 0).all()
        assert out["rvol"].iloc[0] == 0.0  # no prior sessions yet

    def test_ib_flag_and_opening_range_follow_the_clock(self):
        m1, m5 = synth(minutes=90)
        out = build(m1, m5)
        first = out.iloc[:18]  # first session, 09:15 .. 10:40
        assert not first["ib_done"].iloc[11] and first["ib_done"].iloc[12]
        assert first["orb_high"].iloc[:3].isna().all() and first["orb_high"].iloc[3:].notna().all()

    def test_is_causal_dropping_the_last_bar_changes_no_earlier_row(self):
        m1, m5 = synth()
        full = build(m1, m5)
        cut5 = m5.iloc[:-1]
        cut1 = m1[m1.index < m5.index[-1]]
        part = build(cut1, cut5)
        pd.testing.assert_frame_equal(part, full.iloc[: len(part)])

    def test_output_feeds_the_strategy_core(self):
        m1, m5 = synth()
        signals = compute_signals(build(m1, m5))
        assert isinstance(signals, pd.DataFrame)
        assert "code" in signals.columns
