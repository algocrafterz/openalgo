"""The recorded eligibility rules must describe what the run actually applies.

The price floor is a platform default, but it is skipped for the split-adjusted daily panel: its
prices are back-adjusted, not the traded price. A swing run must say so in the ledger, in the run's
membership record and in the G6.2 evidence, not carry a floor it never applied.
"""

from __future__ import annotations

import pandas as pd
import pytest

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.research.intake import execution, profiles
from signal_engine.tests.intake_fixtures import make_spec
from signal_engine.tests.ranked_fixtures import ranked_frames
from signal_engine.tests.test_data_session_integration import (
    evaluate,
    gate,
    ledgers_with_not_eligible,
    step5_fields,
)

PROFILE = profiles.load_profile("flattrade-nse-cash")
CFG = {
    "eligibility_min_prior_sessions": 2,
    "eligibility_min_volume": 1,
    "eligibility_min_price": 10.0,
}


def resolved(spec):
    return {v.field: v for v in execution.resolve_execution(spec, PROFILE).values}


class TestTheLedgerSaysWhatApplies:
    def swing(self):
        return make_spec(style="swing", direction="long_only", timeframe="1d", product="CNC")

    def test_a_swing_spec_records_that_the_floor_is_not_applied(self):
        row = resolved(self.swing())["execution.eligibility_min_price"]
        assert "not applied" in row.reason.lower()
        assert "split-adjusted" in row.reason

    def test_the_reason_names_the_filters_that_do_apply(self):
        reason = resolved(self.swing())["execution.eligibility_min_price"].reason
        for rule in ("listing age", "non-zero volume"):
            assert rule in reason

    def test_the_numeric_default_is_kept_so_the_fingerprint_does_not_move(self):
        row = resolved(self.swing())["execution.eligibility_min_price"]
        assert row.value == execution.ELIGIBILITY_MIN_PRICE

    def test_an_intraday_spec_keeps_the_plain_reason(self):
        row = resolved(make_spec())["execution.eligibility_min_price"]
        assert "not applied" not in row.reason.lower()
        assert "penny" in row.reason

    def test_the_other_eligibility_rows_are_unchanged_for_swing(self):
        rows = resolved(self.swing())
        assert rows["execution.eligibility_min_prior_sessions"].value == 60
        assert "listing age" in rows["execution.eligibility_min_prior_sessions"].reason
        assert rows["execution.eligibility_min_volume"].value == 1


class TestTheRunRecordsWhatWasApplied:
    def layer(self, adjusted):
        frames = ranked_frames(n_sym=3, n=12)
        return rr._build_data_layer(frames, DataCollector(), CFG, adjusted_prices=adjusted)

    def test_adjusted_daily_data_records_the_floor_as_not_applied(self):
        m = self.layer(True).membership
        assert m["price_floor_applied"] is False
        assert "split-adjusted" in m["price_floor_note"]

    def test_raw_data_records_the_floor_as_applied(self):
        m = self.layer(False).membership
        assert m["price_floor_applied"] is True
        assert m["price_floor_note"] is None

    def test_the_policy_values_themselves_are_unchanged(self):
        assert self.layer(True).membership["eligibility_policy"] == {
            "min_prior_sessions": 2,
            "min_volume": 1.0,
            "min_price": 10.0,
        }

    def test_a_penny_priced_adjusted_symbol_is_not_dropped_for_price(self):
        frames = ranked_frames(n_sym=2, n=12)
        penny = frames["S00"].copy()
        for col in ("Open", "High", "Low", "Close"):
            penny[col] = penny[col] / 100.0  # adjusted well below Rs 10
        frames["S00"] = penny
        layer = rr._build_data_layer(frames, DataCollector(), CFG, adjusted_prices=True)
        assert "PRICE_FLOOR" not in {r for d in layer.dropped.values() for r in d.values()}
        raw = rr._build_data_layer(
            {"S00": penny, "S01": frames["S01"]}, DataCollector(), CFG, adjusted_prices=False
        )
        assert "PRICE_FLOOR" in {r for d in raw.dropped.values() for r in d.values()}


class TestTheEvidenceSaysWhatApplied:
    def membership(self, applied):
        m = dict(step5_fields()["membership"])
        m["price_floor_applied"] = applied
        m["price_floor_note"] = None if applied else "not applied to the split-adjusted daily panel"
        return m

    def test_g62_names_the_floor_as_not_applied_for_adjusted_data(self):
        row = gate(
            evaluate(membership=self.membership(False), ledgers=ledgers_with_not_eligible()),
            "G6.2",
        )
        text = row["evidence"]
        assert "min_price not applied" in text
        assert "min_price=10.0" not in text
        assert "min_prior_sessions=2" in text

    def test_g62_keeps_the_floor_in_the_policy_when_it_was_applied(self):
        row = gate(
            evaluate(membership=self.membership(True), ledgers=ledgers_with_not_eligible()),
            "G6.2",
        )
        assert "min_price=10.0" in row["evidence"]

    def test_a_record_without_the_flag_reads_as_before(self):
        row = gate(
            evaluate(membership=step5_fields()["membership"], ledgers=ledgers_with_not_eligible()),
            "G6.2",
        )
        assert "min_price=10.0" in row["evidence"]
