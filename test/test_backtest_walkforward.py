"""Tests for walk-forward analysis and train/test split."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backtest.walkforward import split_temporal, walk_forward_windows, detect_overfitting


class TestSplitTemporal:
    def test_70_30_split(self):
        """70/30 split of a 12-month period."""
        train, test = split_temporal("2025-01-01", "2026-01-01", train_pct=0.7)
        # 365 days * 0.7 = 255 days -> Jan 1 + 255 = Sep 13
        assert train == ("2025-01-01", "2025-09-13")
        assert test == ("2025-09-14", "2026-01-01")

    def test_50_50_split(self):
        """50/50 split."""
        train, test = split_temporal("2025-01-01", "2026-01-01", train_pct=0.5)
        # 365 days * 0.5 = 182 days from Jan 1 = Jul 2
        assert train[0] == "2025-01-01"
        assert test[1] == "2026-01-01"
        # Train end should be 1 day before test start
        from datetime import datetime, timedelta
        train_end = datetime.strptime(train[1], "%Y-%m-%d")
        test_start = datetime.strptime(test[0], "%Y-%m-%d")
        assert test_start - train_end == timedelta(days=1)

    def test_invalid_pct_raises(self):
        with pytest.raises(ValueError):
            split_temporal("2025-01-01", "2026-01-01", train_pct=0.0)
        with pytest.raises(ValueError):
            split_temporal("2025-01-01", "2026-01-01", train_pct=1.0)


class TestWalkForwardWindows:
    def test_basic_windows(self):
        """12 months with 9/3 train/test and 3-month step."""
        windows = walk_forward_windows(
            "2025-01-01", "2026-01-01",
            train_months=9, test_months=3, step_months=3,
        )
        assert len(windows) >= 1
        for train, test in windows:
            # Each window has (start, end) tuples
            assert len(train) == 2
            assert len(test) == 2
            # Train ends before test starts
            assert train[1] < test[0]

    def test_no_test_overlap(self):
        """Test periods should not overlap."""
        windows = walk_forward_windows(
            "2024-01-01", "2026-01-01",
            train_months=6, test_months=3, step_months=3,
        )
        if len(windows) >= 2:
            for i in range(len(windows) - 1):
                curr_test_end = windows[i][1][1]
                next_test_start = windows[i + 1][1][0]
                # Current test end should be before next test start
                assert curr_test_end <= next_test_start

    def test_single_window_short_period(self):
        """Very short period should produce at least 1 window."""
        windows = walk_forward_windows(
            "2025-01-01", "2026-01-01",
            train_months=9, test_months=3, step_months=3,
        )
        assert len(windows) >= 1


class TestDetectOverfitting:
    def test_no_overfitting(self):
        """OOS expectancy close to IS = no overfitting."""
        result = detect_overfitting(
            is_expectancy=0.20, oos_expectancy=0.18,
        )
        assert result["overfitting"] is False
        assert result["ratio"] >= 0.5

    def test_overfitting_detected(self):
        """OOS much worse than IS = overfitting."""
        result = detect_overfitting(
            is_expectancy=0.30, oos_expectancy=-0.10,
        )
        assert result["overfitting"] is True
        assert result["ratio"] < 0.5

    def test_zero_is_expectancy(self):
        """Zero IS expectancy should not crash."""
        result = detect_overfitting(
            is_expectancy=0.0, oos_expectancy=0.0,
        )
        assert result["overfitting"] is False
