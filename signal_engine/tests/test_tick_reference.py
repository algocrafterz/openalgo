"""Dated NSE tick-size reference: regimes, tiers, snapping and the engine's use of them."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from signal_engine.backtest import tick_reference as tr
from signal_engine.features.instruments import round_to_safe_tick, safe_tick, tick_for_price

REF = tr.load()


class TestRegimes:
    def test_the_boundaries_between_regimes_are_exact(self):
        # 2024-06-09 is the last flat Rs 0.05 day, 2024-06-10 the first two-tier day
        assert tr.tick_for_price(100.0, date(2024, 6, 9), REF) == 0.05
        assert tr.tick_for_price(100.0, date(2024, 6, 10), REF) == 0.01
        # 2025-04-14 is the last two-tier day, 2025-04-15 the first six-tier day
        assert tr.tick_for_price(2000.0, date(2025, 4, 14), REF) == 0.05
        assert tr.tick_for_price(2000.0, date(2025, 4, 15), REF) == 0.10

    def test_before_the_two_tier_regime_every_price_is_five_paise(self):
        for price in (0.5, 99.0, 249.99, 250.0, 3000.0, 25000.0):
            assert tr.tick_for_price(price, date(2020, 3, 25), REF) == 0.05

    def test_two_tier_regime(self):
        d = date(2024, 12, 2)
        assert tr.tick_for_price(249.99, d, REF) == 0.01
        assert tr.tick_for_price(250.0, d, REF) == 0.05
        assert tr.tick_for_price(5000.0, d, REF) == 0.05

    @pytest.mark.parametrize(
        ("price", "tick"),
        [(249.99, 0.01), (250.0, 0.05), (1000.0, 0.05), (1000.01, 0.10), (5000.0, 0.10),
         (5000.01, 0.50), (10000.0, 0.50), (10000.01, 1.0), (20000.0, 1.0), (20000.01, 5.0)],
    )  # fmt: skip
    def test_six_tier_regime_edges_belong_to_the_finer_tier(self, price, tick):
        assert tr.tick_for_price(price, date(2026, 1, 5), REF) == tick

    def test_the_current_regime_agrees_with_the_executor_table(self):
        """The live executor uses features.instruments; the reference must not drift from it."""
        today = date(2026, 10, 12)
        for price in (5.0, 249.99, 250.0, 999.0, 1000.0, 1000.01, 4999.0, 7000.0, 15000.0, 50000.0):
            assert tr.tick_for_price(price, today, REF) == tick_for_price(price), price
            assert tr.safe_tick(price, today, REF) == safe_tick(price), price

    def test_a_date_before_coverage_has_no_tick_and_is_never_assumed(self):
        with pytest.raises(tr.NotCovered, match="2015-12-31"):
            tr.tick_for_price(100.0, date(2015, 12, 31), REF)

    def test_a_non_positive_price_is_refused(self):
        for bad in (0, -1.0, float("nan")):
            with pytest.raises(ValueError, match="price"):
                tr.tick_for_price(bad, date(2026, 1, 5), REF)


class TestSafeTickAndSnapping:
    def test_the_safe_tick_allows_for_a_tier_move_within_the_month(self):
        # 200 * 1.5 = 300, so a Rs 200 stock could be Rs 0.05 next month; the safe grid is 0.05
        assert tr.safe_tick(200.0, date(2026, 1, 5), REF) == 0.05
        # before 2024-06-10 everything is already 0.05
        assert tr.safe_tick(200.0, date(2020, 3, 25), REF) == 0.05

    def test_a_2020_stop_is_snapped_to_the_five_paise_grid_not_todays_one_paise_grid(self):
        stop = 155.51
        assert round_to_safe_tick(stop, "up") == 155.51  # today's table: 0.01 below Rs 250
        assert tr.round_to_safe_tick(stop, "up", date(2020, 3, 25), REF) == 155.55
        assert tr.round_to_safe_tick(stop, "down", date(2020, 3, 25), REF) == 155.50

    def test_the_snap_in_the_current_regime_equals_the_executor(self):
        today = date(2026, 10, 12)
        for price in (12.34, 166.66, 2079.7, 6543.21):
            for direction in ("nearest", "up", "down"):
                assert tr.round_to_safe_tick(price, direction, today, REF) == round_to_safe_tick(
                    price, direction
                ), (price, direction)

    def test_a_snapped_price_is_on_the_grid_of_the_exact_tier(self):
        day = date(2025, 6, 2)
        for price in (87.31, 249.0, 251.17, 1203.3, 4999.97):
            snapped = tr.round_to_safe_tick(price, "up", day, REF)
            assert tr.on_grid(snapped, tr.tick_for_price(price, day, REF))


class TestTickgridUsesTheDatedReference:
    def test_snap_stop_with_a_day_uses_that_days_regime(self):
        from signal_engine.backtest import tickgrid

        assert tickgrid.snap_stop(155.51, 1, 1.0, date(2020, 3, 25)) == pytest.approx(155.55)
        assert tickgrid.snap_stop(155.51, 1) == pytest.approx(155.51)  # no day: today's table

    def test_the_evidence_validates_each_stop_against_its_own_date(self):
        from signal_engine.backtest import tickgrid
        from signal_engine.tests.test_tickgrid import _T

        old = _T("A", date(2020, 3, 25), 155.50)
        bad = _T("A", date(2020, 3, 25), 155.51)  # valid today, not in 2020
        closes = {"A": {"2020-02": 160.0}}
        ev = tickgrid.tick_grid_evidence([old, bad], closes)
        assert (ev["stops_checked"], ev["on_grid"], ev["off_grid"]) == (2, 1, 1)
        assert ev["reference"]["version"] == 1 and ev["uncovered"] == 0

    def test_a_stop_before_coverage_is_reported_not_assumed(self):
        from signal_engine.backtest import tickgrid
        from signal_engine.tests.test_tickgrid import _T

        ev = tickgrid.tick_grid_evidence([_T("A", date(2015, 6, 1), 100.0)], {})
        assert ev["uncovered"] == 1 and ev["stops_checked"] == 0


class TestReferenceFileIsSound:
    RAW = yaml.safe_load(
        (Path(tr.__file__).resolve().parent.parent / "research/reference/nse_tick_size.yaml").read_text()
    )

    def test_it_declares_how_it_was_verified(self):
        assert self.RAW["verification"] in ("primary", "secondary")
        assert self.RAW["verified_on"] and self.RAW["sources"]

    def test_regimes_are_contiguous_and_ordered(self):
        spans = [(r["effective_from"], r["effective_to"]) for r in self.RAW["regimes"]]
        assert spans[0][0] == self.RAW["coverage_start"]
        for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
            assert (start - end).days == 1
        assert spans[-1][1] is None

    def test_a_coarser_tick_is_a_multiple_of_every_finer_one(self):
        """So a price on the safe (coarse) grid is on the grid of any finer tier."""
        for regime in self.RAW["regimes"]:
            ticks = [t["tick"] for t in regime["tiers"]]
            assert ticks == sorted(ticks)
            for fine, coarse in zip(ticks, ticks[1:], strict=False):
                assert round(coarse / fine, 6) == round(coarse / fine)

    def test_a_malformed_file_is_refused(self, tmp_path):
        bad = {**self.RAW, "regimes": list(reversed(self.RAW["regimes"]))}
        p = tmp_path / "t.yaml"
        p.write_text(yaml.safe_dump(bad))
        with pytest.raises(tr.TickReferenceError):
            tr.load(p)
