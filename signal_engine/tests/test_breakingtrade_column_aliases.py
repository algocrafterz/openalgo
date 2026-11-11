"""The vendor re-labelled four Market Profile columns on 2026-11-10/11; every poll failed
with 'Unrecognized column set' until they were mapped back. Rows below are real captures."""

from __future__ import annotations

import json
import os

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade.extractor import normalize_table

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "breakingtrade_market_profile_2026_11_11.json"
)

#: label -> (aliased columns of that generation)
_OLD_GENERATION = {
    "1st-hour range % (IB)": "First-hour range %",
    "Unfinished hi / lo": "Unfinished high / low",
    "Price vs prev. day": "Price vs previous-session levels",
}


def _frame(generation: str = "latest") -> pd.DataFrame:
    with open(_FIXTURE, encoding="utf-8") as fh:
        frame = pd.DataFrame(json.load(fh))
    if generation == "2026-11-10":
        frame = frame.rename(columns=_OLD_GENERATION)
    return frame


@pytest.mark.parametrize("generation", ["latest", "2026-11-10"])
def test_renamed_columns_normalize(generation):
    snap = normalize_table(_frame(generation))
    assert snap.kind == "market_profile"
    out = snap.frame
    assert out["ib_pct"].notna().all()
    assert out["tpo_pos"].notna().any()
    assert out["tpo_pos_prev"].isin(["above_pdh", "in_pdr"]).all()


def test_values_parse_as_before():
    out = normalize_table(_frame()).frame.set_index("symbol")
    row = out.loc["360ONE"]
    assert row["opening"] == "gap_up"
    assert row["open_type"] == "open_drive" and row["open_type_dir"] == "up"
    assert row["day_type"] == "~Trend" and row["day_type_dir"] == "up"
    assert row["tpo_pos"] == "in_va"
    assert row["tpo_pos_prev"] == "above_pdh"
    assert row["ib_pct"] == pytest.approx(86.35)


def test_original_names_still_work():
    frame = _frame().rename(
        columns={
            "1st-hour range % (IB)": "IB %",
            "Unfinished hi / lo": "Poor H/L",
            "Price vs today’s profile": "TPO Pos",
            "Price vs prev. day": "TPO Pos (Prev)",
        }
    )
    assert normalize_table(frame).kind == "market_profile"


def test_unknown_single_print_value_is_none_not_an_error():
    out = normalize_table(_frame()).frame
    assert out["single_print"].isna().sum() >= 1
