"""
Walk-forward analysis and train/test temporal split.

Provides tools to detect overfitting by comparing in-sample vs
out-of-sample performance across rolling time windows.

Usage:
    from backtest.walkforward import split_temporal, detect_overfitting

    # Simple 70/30 split
    train, test = split_temporal("2025-01-01", "2026-01-01", train_pct=0.7)
"""

from datetime import datetime, timedelta


def split_temporal(
    start_date: str, end_date: str, train_pct: float = 0.7,
) -> tuple[tuple[str, str], tuple[str, str]]:
    """
    Split a date range into train and test periods chronologically.

    Args:
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        train_pct: Fraction of period for training (0 < pct < 1).

    Returns:
        ((train_start, train_end), (test_start, test_end))

    Raises:
        ValueError: If train_pct is not in (0, 1).
    """
    if train_pct <= 0 or train_pct >= 1:
        raise ValueError(f"train_pct must be between 0 and 1 exclusive, got {train_pct}")

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    total_days = (end - start).days

    train_days = int(total_days * train_pct)
    train_end = start + timedelta(days=train_days)
    test_start = train_end + timedelta(days=1)

    return (
        (start.strftime("%Y-%m-%d"), train_end.strftime("%Y-%m-%d")),
        (test_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    )


def detect_overfitting(
    is_expectancy: float,
    oos_expectancy: float,
    threshold: float = 0.5,
) -> dict:
    """
    Detect overfitting by comparing IS vs OOS expectancy.

    Args:
        is_expectancy: In-sample expectancy (R/trade).
        oos_expectancy: Out-of-sample expectancy (R/trade).
        threshold: OOS/IS ratio below which overfitting is flagged.

    Returns:
        Dict with keys: overfitting (bool), ratio (float), message (str).
    """
    if is_expectancy <= 0:
        return {
            "overfitting": False,
            "ratio": 0.0,
            "message": "IS expectancy is non-positive - no edge to overfit",
        }

    ratio = oos_expectancy / is_expectancy
    overfitting = ratio < threshold

    if overfitting:
        message = (
            f"Overfitting detected: OOS/IS ratio {ratio:.2f} "
            f"(OOS {oos_expectancy:+.3f}R vs IS {is_expectancy:+.3f}R)"
        )
    else:
        message = (
            f"Edge appears robust: OOS/IS ratio {ratio:.2f} "
            f"(OOS {oos_expectancy:+.3f}R vs IS {is_expectancy:+.3f}R)"
        )

    return {
        "overfitting": overfitting,
        "ratio": round(ratio, 2),
        "message": message,
    }
