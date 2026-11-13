"""A vendor page change must stop the poll loudly, never store a snapshot of empty columns."""

from __future__ import annotations

import json
import os

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade.extractor import (
    SnapshotFormatError,
    normalize_table,
)

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "breakingtrade_market_profile_2026_11_11.json"
)


def _frame(rows: int = 30) -> pd.DataFrame:
    with open(_FIXTURE, encoding="utf-8") as fh:
        frame = pd.DataFrame(json.load(fh))
    return pd.concat([frame] * (rows // len(frame) + 1), ignore_index=True).head(rows)


def test_real_capture_passes_canary():
    assert normalize_table(_frame()).kind == "market_profile"


def test_reworded_tpo_pos_alone_is_accepted():
    """BUG-021 Option B: tpo_pos is no longer a required column, so its rewording is tolerated."""
    frame = _frame()
    frame["TPO Pos"] = "some new wording nobody maps"
    assert normalize_table(frame).kind == "market_profile"


def test_reworded_cells_are_refused():
    frame = _frame()
    frame["TPO Pos (Prev)"] = "some new wording nobody maps"
    frame["Day Type"] = "new day type wording"
    with pytest.raises(SnapshotFormatError, match="parsed empty"):
        normalize_table(frame)


def test_unknown_column_names_missing_and_new():
    frame = _frame().rename(columns={"Tail": "Tail Shape v2"})
    with pytest.raises(SnapshotFormatError) as err:
        normalize_table(frame)
    assert "Tail" in str(err.value)
    assert "Tail Shape v2" in str(err.value)


def test_small_table_skips_canary():
    frame = _frame(rows=5)
    frame["TPO Pos"] = "unmapped"
    normalize_table(frame)
