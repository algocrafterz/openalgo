"""Engine tick-grid snapping: the executor's stop rounding, trails and the harness carry."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from signal_engine.backtest import indicators, tickgrid
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig

SIGNAL_MINS = 9 * 60 + 20


class _Dip(Strategy):
    """One entry at the 09:20 close, with a chosen stop, then whatever the frame does."""

    name = "dip"

    def __init__(self, sl, direction=1, trail_to=None):
        self.sl, self.direction, self.trail_to = sl, direction, trail_to

    def prepare(self, df, p):
        return indicators.add_session_columns(df)

    def entry(self, c, i, p, direction):
        if direction != self.direction or c["mins"][i] != SIGNAL_MINS:
            return None
        return EntrySignal(
            direction, self.sl, float(c["Close"][i]) * (2 if direction == 1 else 0.5)
        )

    def trail(self, c, i, p, pos):
        return self.trail_to if self.trail_to is not None and i > pos.entry_bar else None


def _frame(price, lows=None, highs=None, factor=None, bars=75):
    index = pd.date_range("2026-02-03 09:15", periods=bars, freq="5min", tz="Asia/Kolkata")
    f = pd.DataFrame(
        {
            "Open": price,
            "High": price + 0.0,
            "Low": price + 0.0,
            "Close": price,
            "Volume": 1000.0,
        },
        index=index,
    )
    for bar, value in (lows or {}).items():
        f.iloc[bar, f.columns.get_loc("Low")] = value
    for bar, value in (highs or {}).items():
        f.iloc[bar, f.columns.get_loc("High")] = value
    if factor is not None:
        f["price_factor"] = factor
    return f


def _run(**kw):
    return RunConfig(skip_open_minutes=0, entry_cutoff_min=11 * 60, min_sl_pct=0.002, **kw)


def _trades(strategy, frame, run):
    factor = frame["price_factor"] if "price_factor" in frame.columns else None
    prepared = tickgrid.carry_price_factor(strategy.prepare(frame.copy(), None), factor)
    ctx = Ctx(prepared, symbol="X")
    return simulate(ctx, strategy, None, run)


class TestEngineSnapping:
    PRICE = 2000.0  # safe tick 0.10

    def test_the_default_is_the_legacy_engine(self):
        t = _trades(_Dip(1980.03), _frame(self.PRICE, lows={6: 1980.0}), _run())
        assert t[0].sl == 1980.03
        assert t[0].reason == "SL"

    def test_long_stop_is_rounded_up_and_fires_on_the_snapped_trigger(self):
        # the low 1980.08 never touches the raw stop 1980.03, but touches the snapped 1980.10
        frame = _frame(self.PRICE, lows={6: 1980.08})
        legacy = _trades(_Dip(1980.03), frame, _run())
        snapped = _trades(_Dip(1980.03), frame, _run(tick_grid="safe_tick"))
        assert legacy[0].reason != "SL"
        assert snapped[0].sl == pytest.approx(1980.10)
        assert snapped[0].reason == "SL"
        assert snapped[0].exit == pytest.approx(1980.10)

    def test_short_stop_is_rounded_down(self):
        frame = _frame(self.PRICE, highs={6: 2019.95})
        t = _trades(_Dip(2020.07, direction=-1), frame, _run(tick_grid="safe_tick"))
        assert t[0].sl == pytest.approx(2020.00)
        assert t[0].reason != "SL"  # 2019.95 is below the snapped trigger
        t = _trades(
            _Dip(2020.07, direction=-1),
            _frame(self.PRICE, highs={6: 2020.0}),
            _run(tick_grid="safe_tick"),
        )
        assert t[0].reason == "SL"

    def test_risk_and_min_sl_pct_read_the_signal_stop_not_the_snapped_one(self):
        t = _trades(_Dip(1980.03), _frame(self.PRICE), _run(tick_grid="safe_tick"))
        assert t[0].risk == pytest.approx(self.PRICE - 1980.03)
        # a raw stop 0.15% away is rejected by min_sl_pct 0.2% whatever the snap would do
        assert _trades(_Dip(1997.0), _frame(self.PRICE), _run(tick_grid="safe_tick")) == []

    def test_a_stop_on_the_grid_is_unchanged(self):
        t = _trades(_Dip(1980.10), _frame(self.PRICE), _run(tick_grid="safe_tick"))
        assert t[0].sl == pytest.approx(1980.10)

    def test_a_trail_is_snapped_and_still_only_tightens(self):
        frame = _frame(self.PRICE)
        t = _trades(_Dip(1980.03, trail_to=1990.01), frame, _run(tick_grid="safe_tick"))
        # trailed to 1990.01 -> 1990.10 on the grid, which the flat frame does not touch
        assert t[0].reason != "SL"
        t2 = _trades(
            _Dip(1980.03, trail_to=1990.01),
            _frame(self.PRICE, lows={8: 1990.05}),
            _run(tick_grid="safe_tick"),
        )
        assert t2[0].reason == "SL"
        assert t2[0].exit == pytest.approx(1990.10)

    def test_the_snap_is_made_on_raw_prices_for_an_adjusted_frame(self):
        factor = 0.5
        frame = _frame(self.PRICE * factor, lows={6: 1980.08 * factor}, factor=factor)
        t = _trades(_Dip(1980.03 * factor), frame, _run(tick_grid="safe_tick"))
        assert t[0].sl == pytest.approx(1980.10 * factor)
        assert t[0].reason == "SL"

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="tick_grid"):
            _trades(_Dip(1980.03), _frame(self.PRICE), _run(tick_grid="nearest"))


class TestHarnessCarriesThePriceFactor:
    def test_prepare_drops_the_column_and_the_harness_puts_it_back(self):
        from signal_engine.backtest.harness import Backtest

        factor = 0.5
        frame = _frame(1000.0, lows={6: 990.04}, factor=factor)  # raw 1980.08
        strategy = _Dip(1980.03 * factor)
        assert "price_factor" not in strategy.prepare(frame.copy(), None).columns
        bt = Backtest(
            strategy, {"X": frame}, _run(tick_grid="safe_tick"), release_raw=True, single_pass=True
        )
        trades = bt.trades(None)
        assert trades[0].sl == pytest.approx(1980.10 * factor)
        assert trades[0].reason == "SL"

    def test_a_raw_frame_is_left_alone(self):
        prepared = _Dip(1.0).prepare(_frame(100.0), None)
        assert tickgrid.carry_price_factor(prepared, None) is prepared

    def test_a_bar_without_a_factor_is_refused(self):
        frame = _frame(100.0, bars=10)
        prepared = _Dip(1.0).prepare(frame.copy(), None)
        partial = pd.Series(1.0, index=frame.index[:5])
        with pytest.raises(ValueError, match="price_factor"):
            tickgrid.carry_price_factor(prepared, partial)

    def test_in_place_attach_adds_the_column_to_the_given_frames(self):
        frames = {"A": _frame(100.0, bars=10)}
        out = tickgrid.attach_price_factors(frames, {"A": {date(2026, 2, 3): 0.8}}, in_place=True)
        assert out["A"] is frames["A"]
        assert (frames["A"]["price_factor"] == 0.8).all()


class TestEngineSnapsOnTheGridOfTheTradesOwnDate:
    """The exchange tick was Rs 0.05 for every stock before 2024-06-10; a 2020 backtest must not
    model a Rs 0.01 trigger."""

    def _frame_on(self, start):
        f = _frame(100.0, lows={6: 90.0})
        f.index = pd.date_range(f"{start} 09:15", periods=len(f), freq="5min", tz="Asia/Kolkata")
        return f

    def test_a_2020_stop_lands_on_the_five_paise_grid(self):
        t = _trades(_Dip(99.53), self._frame_on("2020-03-25"), _run(tick_grid="safe_tick"))
        assert t[0].sl == pytest.approx(99.55)

    def test_a_2026_stop_of_the_same_price_lands_on_the_one_paise_grid(self):
        t = _trades(_Dip(99.53), self._frame_on("2026-02-03"), _run(tick_grid="safe_tick"))
        assert t[0].sl == pytest.approx(99.53)

    def test_the_recorded_evidence_agrees_with_what_the_engine_did(self):
        for start in ("2020-03-25", "2026-02-03"):
            t = _trades(_Dip(99.53), self._frame_on(start), _run(tick_grid="safe_tick"))
            closes = {"X": {f"{start[:4]}-{int(start[5:7]) - 1:02d}": 100.0}}
            ev = tickgrid.tick_grid_evidence(t, closes, engine_mode="safe_tick")
            assert ev["off_grid"] == 0 and ev["stops_checked"] == 1, start
