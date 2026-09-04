"""MFE/MAE forward-test maths (validate.py).

No network: the part that can silently be wrong is the excursion and first-touch arithmetic,
and that is pure over a bar frame.
"""

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import validate


def _bars(rows):
    """rows: list of (high, low, close)."""
    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-09-03 10:00") + pd.Timedelta(minutes=5 * i),
                "high": h,
                "low": low,
                "close": c,
            }
            for i, (h, low, c) in enumerate(rows)
        ]
    )


# ---------------------------------------------------------------------------
# Excursions
# ---------------------------------------------------------------------------


def test_long_excursions_measure_from_entry():
    bars = _bars([(104, 99, 103), (106, 102, 105)])
    out = validate.excursions(bars, entry=100.0, direction="up", r_value=2.0)
    assert out["mfe_r"] == pytest.approx(3.0)  # high 106 -> +6 -> 3R
    assert out["mae_r"] == pytest.approx(0.5)  # low 99 -> -1 -> 0.5R


def test_short_excursions_invert_favourable_and_adverse():
    """For a 'down' scan, favourable means price FELL."""
    bars = _bars([(101, 94, 95)])
    out = validate.excursions(bars, entry=100.0, direction="down", r_value=2.0)
    assert out["mfe_r"] == pytest.approx(3.0)  # low 94 -> 6 in favour
    assert out["mae_r"] == pytest.approx(0.5)  # high 101 -> 1 against


def test_empty_window_yields_no_measurement():
    out = validate.excursions(_bars([]), entry=100.0, direction="up", r_value=2.0)
    assert out["mfe_r"] is None and out["outcome"] is None


# ---------------------------------------------------------------------------
# First touch
# ---------------------------------------------------------------------------


def test_target_hit_first_is_a_win():
    bars = _bars([(100.5, 99.5, 100), (103, 100, 102)])
    assert validate._first_touch(bars, 100.0, "up", 2.0) == "+1R"


def test_stop_hit_first_is_a_loss():
    bars = _bars([(100.5, 97.5, 98), (103, 100, 102)])
    assert validate._first_touch(bars, 100.0, "up", 2.0) == "-1R"


def test_bar_touching_both_counts_as_the_loss():
    """OHLC cannot reveal the path within a bar, so assume the worse ordering rather than
    flattering the result."""
    bars = _bars([(103, 97, 100)])
    assert validate._first_touch(bars, 100.0, "up", 2.0) == "-1R"


def test_neither_touched_is_unresolved():
    bars = _bars([(101, 99, 100)])
    assert validate._first_touch(bars, 100.0, "up", 2.0) is None


def test_first_touch_direction_is_respected():
    bars = _bars([(100.5, 97.5, 98)])
    # falling 2 points is the TARGET for a short, the stop for a long
    assert validate._first_touch(bars, 100.0, "down", 2.0) == "+1R"
    assert validate._first_touch(bars, 100.0, "up", 2.0) == "-1R"


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def test_atr_is_positive_over_real_ranges():
    assert validate.average_true_range(_bars([(102, 98, 100), (104, 100, 103)])) > 0


def test_atr_needs_two_bars():
    assert validate.average_true_range(_bars([(102, 98, 100)])) is None


def test_atr_of_flat_bars_is_none():
    """A zero ATR would divide by zero downstream, so it must not be returned."""
    assert validate.average_true_range(_bars([(100, 100, 100), (100, 100, 100)])) is None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summarize_reports_ratio_and_win_rate():
    measured = pd.DataFrame(
        [
            {"scan": "A", "mfe_r_30": 2.0, "mae_r_30": 1.0, "outcome_30": "+1R"},
            {"scan": "A", "mfe_r_30": 4.0, "mae_r_30": 1.0, "outcome_30": "-1R"},
            {"scan": "A", "mfe_r_30": 3.0, "mae_r_30": 1.0, "outcome_30": "+1R"},
        ]
    )
    summary = validate.summarize(measured, 30).iloc[0]
    assert summary["n"] == 3
    assert summary["median_mfe_r"] == 3.0
    assert summary["mfe_mae_ratio"] == 3.0
    assert summary["win_rate_1r"] == pytest.approx(0.67, abs=0.01)


def test_summarize_handles_unresolved_hits():
    measured = pd.DataFrame([{"scan": "A", "mfe_r_30": 1.0, "mae_r_30": 1.0, "outcome_30": None}])
    summary = validate.summarize(measured, 30).iloc[0]
    assert summary["resolved"] == 0
    # pandas coerces the missing rate to NaN inside a numeric Series
    assert pd.isna(summary["win_rate_1r"])


# ---------------------------------------------------------------------------
# Control sample
# ---------------------------------------------------------------------------


def test_control_sample_mirrors_count_and_timestamps():
    hits = pd.DataFrame(
        {
            "captured_at": pd.to_datetime(["2026-09-03 10:16", "2026-09-03 10:46"]),
            "scan": ["A", "B"],
            "direction": ["up", "down"],
            "symbol": ["AAA", "BBB"],
            "is_new": [1, 1],
        }
    )
    control = validate.control_sample(hits, ["AAA", "BBB", "CCC"])
    assert len(control) == len(hits)
    assert list(control["captured_at"]) == list(hits["captured_at"])
    assert set(control["scan"]) == {"RANDOM CONTROL"}
