"""Tests for parameter sensitivity analysis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from backtest.sensitivity import sensitivity_sweep, stability_score


class TestSensitivitySweep:
    def test_returns_dataframe(self):
        """Sweep should return DataFrame with correct columns."""
        # Use a mock runner that returns predictable results
        results = {1.0: 0.15, 1.5: 0.10, 2.0: -0.05}

        def mock_run(value):
            return {"expectancy_r": results[value], "total_trades": 100}

        df = sensitivity_sweep(
            param_name="tp_multiplier",
            values=[1.0, 1.5, 2.0],
            run_fn=mock_run,
        )
        assert isinstance(df, pd.DataFrame)
        assert "param_value" in df.columns
        assert "expectancy_r" in df.columns
        assert "total_trades" in df.columns
        assert len(df) == 3

    def test_sweep_preserves_values(self):
        """Sweep should include all tested values."""
        values = [0.8, 1.0, 1.2, 1.5]

        def mock_run(value):
            return {"expectancy_r": 0.1 * value, "total_trades": 50}

        df = sensitivity_sweep(
            param_name="tp_multiplier",
            values=values,
            run_fn=mock_run,
        )
        assert list(df["param_value"]) == values


class TestStabilityScore:
    def test_high_stability(self):
        """Low CV = stable parameter."""
        metrics = [0.10, 0.11, 0.09, 0.10]  # CV ~ 0.08
        score = stability_score(metrics)
        assert score["stable"] is True
        assert score["cv"] < 0.5

    def test_low_stability(self):
        """High CV = fragile parameter."""
        metrics = [0.50, -0.30, 0.10, -0.20]  # wildly varying
        score = stability_score(metrics)
        assert score["stable"] is False
        assert score["cv"] > 0.5

    def test_zero_mean(self):
        """Zero mean metrics should handle gracefully."""
        metrics = [0.1, -0.1, 0.1, -0.1]
        score = stability_score(metrics)
        # CV is undefined with zero mean, but should not crash
        assert "cv" in score
