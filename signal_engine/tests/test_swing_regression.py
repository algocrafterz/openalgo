"""Existing swing strategies must behave exactly as they did before the ranked-portfolio engine work.

The golden file was produced from the engine as it stood before those changes. A difference here
means the shared swing engine, the metrics or the run record changed for stop-based strategies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from signal_engine.tests.swing_fixtures import SWING_KEYS, swing_snapshot

GOLDEN = Path(__file__).parent / "golden" / "swing_regression.json"


@pytest.fixture(scope="module")
def live():
    return json.loads(json.dumps(swing_snapshot(), sort_keys=True))


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", SWING_KEYS)
def test_trades_are_unchanged(live, golden, key):
    assert live[key]["trades"] == golden[key]["trades"]
    assert live[key]["sha"] == golden[key]["sha"]


@pytest.mark.parametrize("key", SWING_KEYS)
def test_headline_metrics_and_series_are_unchanged(live, golden, key):
    assert live[key].get("metrics") == golden[key].get("metrics")
    assert live[key].get("series_sha") == golden[key].get("series_sha")


def test_the_golden_panel_exercises_every_strategy():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert all(golden[k]["trades"] > 0 for k in ("ibs_meanrev", "gap_rsi", "ema_pullback"))
