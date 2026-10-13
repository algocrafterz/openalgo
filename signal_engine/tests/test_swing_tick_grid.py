"""Swing engine tick-grid snapping: a resting stop is snapped onto the dated exchange grid.

A swing position's stop is a real resting order, so with `tick_grid="safe_tick"` the engine
fills it on the trigger the executor would send, on the grid in force on that bar's date, as
the intraday engine does. The default stays the legacy unsnapped engine.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from signal_engine.backtest import tickgrid
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx
from signal_engine.tests.test_backtest_ledgers import SwingScript

STOP = 95.51  # off the 2020 five-paise grid, on today's one-paise grid
LOW = 95.53  # between the raw stop and the snapped one


def _frame(start: str, factor: float | None = None) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=8, tz="Asia/Kolkata")
    f = pd.DataFrame(
        {"Open": 100.0, "High": 100.2, "Low": 99.9, "Close": 100.0, "Volume": 1000.0}, index=idx
    )
    f.iloc[4, f.columns.get_loc("Low")] = LOW
    f["day"] = pd.Series(f.index.date, index=f.index)
    if factor is not None:
        f["price_factor"] = factor
    return f


def _run(df: pd.DataFrame, stop: float = STOP, **cfg):
    script = SwingScript({2: (1, stop, math.inf)})
    return simulate_swing(Ctx(df, "A"), script, None, SwingRunConfig(**cfg))


class TestSwingSnapping:
    def test_the_default_is_the_legacy_unsnapped_engine(self):
        assert SwingRunConfig().tick_grid == tickgrid.NONE
        trades = _run(_frame("2020-03-02"))
        assert trades and all(t.reason != "SL" for t in trades)  # the low never reaches 95.51

    def test_a_2020_long_stop_is_snapped_up_onto_the_five_paise_grid_and_fires(self):
        (t,) = _run(_frame("2020-03-02"), tick_grid=tickgrid.SAFE_TICK)
        assert t.reason == "SL"
        assert t.sl == pytest.approx(95.55)
        assert t.exit == pytest.approx(95.55)

    def test_risk_reads_the_signal_stop_not_the_snapped_one(self):
        (t,) = _run(_frame("2020-03-02"), tick_grid=tickgrid.SAFE_TICK)
        assert t.risk == pytest.approx(t.entry - STOP)

    def test_a_2026_stop_of_the_same_price_is_already_on_the_one_paise_grid(self):
        trades = _run(_frame("2026-03-02"), tick_grid=tickgrid.SAFE_TICK)
        assert trades and all(t.reason != "SL" for t in trades)
        assert trades[0].sl == pytest.approx(STOP)

    def test_the_snap_is_made_on_raw_prices_for_an_adjusted_frame(self):
        # adjusted = raw * 2: the raw stop 47.755 snaps up to 47.80, adjusted 95.60 (snapping
        # the adjusted price itself would give 95.55)
        (t,) = _run(_frame("2020-03-02", factor=2.0), tick_grid=tickgrid.SAFE_TICK)
        assert t.reason == "SL"
        assert t.exit == pytest.approx(95.60)

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="tick_grid"):
            SwingRunConfig(tick_grid="nearest")

    def test_a_stop_before_the_reference_coverage_is_refused_not_assumed(self):
        with pytest.raises(ValueError, match="2015"):
            _run(_frame("2015-06-01"), tick_grid=tickgrid.SAFE_TICK)


class TestSwingEvidence:
    def test_the_recorded_evidence_agrees_with_what_the_engine_did(self):
        for start in ("2020-03-02", "2026-03-02"):
            df = _frame(start)
            trades = _run(df, tick_grid=tickgrid.SAFE_TICK)
            closes = tickgrid.month_end_closes(tickgrid.session_closes({"A": df}))
            ev = tickgrid.tick_grid_evidence(trades, closes, engine_mode=tickgrid.SAFE_TICK)
            assert ev["stops_checked"] == len(trades) and ev["off_grid"] == 0, start

    def test_the_legacy_engine_is_caught_off_grid_in_2020(self):
        df = _frame("2020-03-02")
        trades = _run(df)
        closes = tickgrid.month_end_closes(tickgrid.session_closes({"A": df}))
        ev = tickgrid.tick_grid_evidence(trades, closes, engine_mode=tickgrid.NONE)
        assert ev["off_grid"] == 1
