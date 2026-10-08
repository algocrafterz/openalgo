"""Tick grid: rounding parity, the grid check, G6.4 part A evidence and the gate."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import evaluation, tickgrid
from signal_engine.features.instruments import round_to_safe_tick, safe_tick, tick_for_price


class TestRoundingParityWithTheExecutor:
    PRICES = [
        0.5, 9.99, 99.97, 149.99, 166.66, 167.0, 180.04, 249.99, 250.0, 333.33, 666.67,
        999.97, 1000.0, 1000.03, 2079.7, 2079.75, 3333.33, 4999.99, 5000.01, 6543.21,
        9999.99, 10050.5, 13333.3, 20000.0, 20123.45, 54321.12,
    ]  # fmt: skip

    @pytest.mark.parametrize("direction", ["nearest", "down", "up"])
    def test_pure_rounding_equals_executor_round_to_tick(self, direction):
        from signal_engine.executor import round_to_tick

        for price in self.PRICES:
            assert round_to_safe_tick(price, direction) == round_to_tick(price, direction), price


class TestSnapStop:
    def test_long_stop_rounds_up_and_short_stop_rounds_down(self):
        # 1980.03 sits on tick 0.10 (safe tier of 1980 * 1.5 = 2970)
        assert tickgrid.snap_stop(1980.03, 1) == pytest.approx(1980.10)
        assert tickgrid.snap_stop(2020.07, -1) == pytest.approx(2020.00)

    def test_a_price_already_on_the_grid_does_not_move(self):
        assert tickgrid.snap_stop(1980.10, 1) == pytest.approx(1980.10)
        assert tickgrid.snap_stop(1980.10, -1) == pytest.approx(1980.10)

    def test_snaps_on_raw_prices_when_the_frame_is_adjusted(self):
        # raw 1980.03 -> 1980.10; the adjusted frame carries raw * 0.5
        assert tickgrid.snap_stop(1980.03 * 0.5, 1, 0.5) == pytest.approx(1980.10 * 0.5)

    @pytest.mark.parametrize("factor", [0.0, -1.0, math.nan, math.inf])
    def test_a_bad_price_factor_is_refused(self, factor):
        with pytest.raises(ValueError, match="price_factor"):
            tickgrid.snap_stop(100.0, 1, factor)

    def test_a_bad_stop_is_refused(self):
        with pytest.raises(ValueError, match="raw stop"):
            tickgrid.snap_stop(-5.0, 1)


class TestGridCheck:
    def test_tier_edges_belong_to_the_finer_tier(self):
        assert tick_for_price(249.99) == 0.01
        # 250.00 itself is in the 0.05 band in the code, although the docstring says an edge
        # belongs to the finer tier; only the upper edges (1000, 5000, ...) follow that rule
        assert tick_for_price(250.0) == 0.05
        assert tick_for_price(1000.0) == 0.05
        assert tick_for_price(1000.01) == 0.10

    def test_on_grid_tolerates_float_noise_only(self):
        assert tickgrid.on_grid(2079.7, 0.1)
        assert tickgrid.on_grid(20796.999999999996 / 10, 0.1)
        assert not tickgrid.on_grid(2079.75, 0.1)
        assert not tickgrid.on_grid(100.004, 0.01)

    def test_previous_month_wraps_the_year(self):
        assert tickgrid.previous_month_key("2026-01") == "2025-12"
        assert tickgrid.previous_month_key("2026-03") == "2026-02"

    def test_month_end_close_is_the_last_session_of_each_month(self):
        got = tickgrid.month_end_closes(
            {"A": {date(2026, 1, 5): 10.0, date(2026, 1, 30): 12.0, date(2026, 2, 2): 99.0}}
        )
        assert got == {"A": {"2026-01": 12.0, "2026-02": 99.0}}


def _bars(price=100.0, bars=10):
    index = pd.date_range("2026-02-03 09:15", periods=bars, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {"Open": price, "High": price, "Low": price, "Close": price, "Volume": 1000.0}, index=index
    )


class _T:
    def __init__(self, symbol, day, sl, direction=1):
        self.symbol, self.day, self.sl, self.direction = symbol, day, sl, direction


class TestEvidence:
    CLOSES = {"A": {"2026-01": 1500.0}, "B": {"2026-01": 100.0}}

    def test_off_grid_stops_are_counted_by_tier_and_named(self):
        trades = [_T("A", date(2026, 2, 3), 2079.75), _T("B", date(2026, 2, 3), 99.37)]
        ev = tickgrid.tick_grid_evidence(trades, self.CLOSES)
        assert (ev["stops_checked"], ev["on_grid"], ev["off_grid"]) == (2, 1, 1)
        assert ev["by_tier"]["0.1"] == {"checked": 1, "off_grid": 1}
        assert ev["by_tier"]["0.01"] == {"checked": 1}
        assert ev["worst"][0]["symbol"] == "A"
        assert ev["tick_basis"] == {"exact": 2}

    def test_snapped_stops_pass(self):
        trades = [_T("A", date(2026, 2, 3), tickgrid.snap_stop(2079.75, 1))]
        assert tickgrid.tick_grid_evidence(trades, self.CLOSES)["off_grid"] == 0

    def test_a_month_missing_from_the_data_uses_the_labelled_fallback(self):
        ev = tickgrid.tick_grid_evidence([_T("A", date(2026, 5, 4), 2079.7)], self.CLOSES)
        assert ev["tick_basis"] == {"fallback_safe_tick": 1}

    def test_a_trade_without_a_stop_is_not_counted(self):
        ev = tickgrid.tick_grid_evidence([_T("A", date(2026, 2, 3), float("nan"))], self.CLOSES)
        assert ev["stops_checked"] == 0

    def test_a_stop_is_checked_against_the_exact_tier_not_safe_tick(self):
        # previous close 100 -> tier 0.01, so 99.37 is on the grid even though safe_tick(99) is 0.01
        ev = tickgrid.tick_grid_evidence([_T("B", date(2026, 2, 3), 99.37)], self.CLOSES)
        assert ev["off_grid"] == 0

    def test_a_split_month_takes_its_tier_from_the_restated_previous_close(self):
        # 5:1 split in February. January closed at adjusted 300 (raw 1500, factor 0.2); February
        # sessions have factor 1. The exchange restates the base price, so the tier is that of
        # 300 (tick 0.05), not that of the raw 1500 (tick 0.10): 286.25 is on grid.
        closes = {"A": {"2026-01": 300.0}}
        factors = {"A": {date(2026, 2, 3): 1.0}}
        ev = tickgrid.tick_grid_evidence([_T("A", date(2026, 2, 3), 286.25)], closes, factors)
        assert ev["off_grid"] == 0 and ev["tick_basis"] == {"exact": 1}
        # the same stop judged against the raw pre-split close would wrongly be off grid
        raw_based = {"A": {"2026-01": 1500.0}}
        assert (
            tickgrid.tick_grid_evidence([_T("A", date(2026, 2, 3), 286.25)], raw_based)["off_grid"]
            == 1
        )

    def test_the_price_factor_moves_the_check_to_raw_prices(self):
        # adjusted 1040.05 with factor 0.5 is raw 2080.10, on tick 0.10
        t = _T("A", date(2026, 2, 3), 1040.05)
        ev = tickgrid.tick_grid_evidence([t], self.CLOSES, {"A": {date(2026, 2, 3): 0.5}})
        assert ev["off_grid"] == 0
        assert tickgrid.tick_grid_evidence([t], self.CLOSES)["off_grid"] == 1


def _inputs(tick_grid):
    return evaluation._Inputs(
        trades=[],
        metrics={},
        n_trials=1,
        horizon="intraday",
        cost_bps=0.0,
        cost_model=None,
        oos_start=None,
        position_caps=(),
        params={},
        tick_grid=tick_grid,
    )


class TestGateG64:
    def _ev(self, **kw):
        base = {"stops_checked": 10, "on_grid": 10, "off_grid": 0, "engine_tick_grid": "safe_tick"}
        return {**base, **kw}

    def test_no_evidence_is_not_evaluated(self):
        assert evaluation._g64(_inputs(None))[0] == "not_evaluated"

    def test_malformed_evidence_fails(self):
        assert evaluation._g64(_inputs({"stops_checked": 3}))[0] == "fail"
        assert evaluation._g64(_inputs(self._ev(on_grid=4)))[0] == "fail"

    def test_off_grid_stops_fail_and_name_the_worst(self):
        ev = self._ev(
            on_grid=7,
            off_grid=3,
            by_tier={"0.1": {"checked": 10, "off_grid": 3}},
            worst=[{"symbol": "TCS", "day": "2026-02-03", "raw_stop": 2079.75, "tick": 0.1}],
        )
        status, value, _, text = evaluation._g64(_inputs(ev))
        assert (status, value) == ("fail", 3)
        assert "TCS" in text and "tick 0.1: 3 of 10" in text

    def test_no_stops_is_not_evaluated(self):
        ev = self._ev(stops_checked=0, on_grid=0)
        assert evaluation._g64(_inputs(ev))[0] == "not_evaluated"

    def test_clean_part_a_never_passes_without_the_broker_check(self):
        status, _, _, text = evaluation._g64(_inputs(self._ev()))
        assert status == "not_evaluated"
        assert "Part B is owed" in text

    def test_the_gate_is_registered_and_hard(self):
        assert "G6.4" in evaluation._EVALUATORS
        assert next(g for g in evaluation._GATES if g[0] == "G6.4")[3] is True


class TestPriceFactorColumns:
    def _frames(self):
        return {"A": _bars()}

    def test_attaches_the_session_factor(self):
        out = tickgrid.attach_price_factors(self._frames(), {"A": {date(2026, 2, 3): 0.8}})
        assert (out["A"]["price_factor"] == 0.8).all()

    def test_a_missing_session_is_refused(self):
        with pytest.raises(ValueError, match="price_factor"):
            tickgrid.attach_price_factors(self._frames(), {"A": {}})

    def test_session_close_is_the_last_bar_close_in_adjusted_prices(self):
        got = tickgrid.session_closes(self._frames())
        assert got == {"A": {date(2026, 2, 3): 100.0}}

    def test_safe_tick_matches_the_instrument_helper(self):
        assert safe_tick(1980.0) == 0.1
        assert np.isclose(round_to_safe_tick(1980.03, "up"), 1980.1)
