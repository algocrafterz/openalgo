"""
Parameter sensitivity analysis.

Sweeps one parameter at a time across a range of values,
runs the backtest for each, and reports how stable the edge is.

Usage:
    from backtest.sensitivity import sensitivity_sweep, stability_score

    df = sensitivity_sweep(
        param_name="tp_multiplier",
        values=[0.8, 1.0, 1.2, 1.5],
        run_fn=lambda v: run_and_evaluate(config_with(tp_multiplier=v)),
    )
"""

from typing import Callable

import numpy as np
import pandas as pd


def sensitivity_sweep(
    param_name: str,
    values: list,
    run_fn: Callable[[object], dict],
) -> pd.DataFrame:
    """
    Sweep a single parameter across values and collect metrics.

    Args:
        param_name: Name of the parameter being swept (for display).
        values: List of values to test.
        run_fn: Callable that takes a parameter value and returns a dict
                with at least 'expectancy_r' and 'total_trades' keys.

    Returns:
        DataFrame with columns: param_value, expectancy_r, total_trades,
        plus any other keys returned by run_fn.
    """
    rows = []
    for val in values:
        result = run_fn(val)
        row = {"param_value": val}
        row.update(result)
        rows.append(row)

    return pd.DataFrame(rows)


def stability_score(metrics: list[float]) -> dict:
    """
    Compute stability of a metric across parameter values.

    Uses coefficient of variation (CV = std/|mean|) to measure
    how much the metric changes. Low CV = stable (robust edge).

    Args:
        metrics: List of metric values (e.g., expectancy_r for each param value).

    Returns:
        Dict with keys: cv (float), stable (bool), message (str).
    """
    arr = np.array(metrics, dtype=float)
    mean = arr.mean()
    std = arr.std()

    if abs(mean) < 1e-10:
        # Mean is effectively zero - CV is undefined
        # Check if all values are near zero
        if std < 0.05:
            return {
                "cv": 0.0,
                "stable": True,
                "message": "Metric is consistently near zero",
            }
        return {
            "cv": float("inf"),
            "stable": False,
            "message": f"Metric oscillates around zero (std={std:.3f}) - sign-unstable",
        }

    cv = std / abs(mean)
    stable = bool(cv < 0.5)

    if stable:
        message = f"Stable (CV={cv:.2f}): metric holds across parameter range"
    else:
        message = f"Fragile (CV={cv:.2f}): metric is sensitive to this parameter"

    return {
        "cv": round(cv, 3),
        "stable": stable,
        "message": message,
    }


def format_sensitivity_report(
    param_name: str,
    sweep_df: pd.DataFrame,
    score: dict,
) -> str:
    """Format sensitivity sweep results into readable text."""
    lines = []
    w = 70
    lines.append(f"\n{'='*w}")
    lines.append(f"  SENSITIVITY: {param_name}")
    lines.append(f"{'='*w}")

    for _, row in sweep_df.iterrows():
        val = row["param_value"]
        exp = row.get("expectancy_r", 0)
        trades = row.get("total_trades", 0)
        bar = "+" * max(0, int(exp * 20)) if exp > 0 else "-" * max(0, int(abs(exp) * 20))
        lines.append(f"  {val:>8}  {exp:+.3f}R  ({int(trades)} trades)  {bar}")

    lines.append(f"\n  {score['message']}")
    lines.append(f"{'='*w}")
    return "\n".join(lines)
