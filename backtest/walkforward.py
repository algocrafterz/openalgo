"""
Walk-forward analysis and train/test temporal split.

Provides tools to detect overfitting by comparing in-sample vs
out-of-sample performance across rolling time windows.

Usage:
    from backtest.walkforward import split_temporal, walk_forward_windows

    # Simple 70/30 split
    train, test = split_temporal("2025-01-01", "2026-01-01", train_pct=0.7)

    # Rolling walk-forward windows
    windows = walk_forward_windows("2024-01-01", "2026-01-01",
                                   train_months=9, test_months=3, step_months=3)
"""

from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta


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


def walk_forward_windows(
    start_date: str,
    end_date: str,
    train_months: int = 9,
    test_months: int = 3,
    step_months: int = 3,
) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """
    Generate rolling walk-forward train/test windows.

    Each window: [train_start, train_end] -> [test_start, test_end]
    Windows step forward by step_months.

    Args:
        start_date: Overall start date (YYYY-MM-DD).
        end_date: Overall end date (YYYY-MM-DD).
        train_months: Training period length in months.
        test_months: Testing period length in months.
        step_months: How far to step forward between windows.

    Returns:
        List of ((train_start, train_end), (test_start, test_end)) tuples.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    windows = []
    cursor = start

    while True:
        train_start = cursor
        train_end = train_start + relativedelta(months=train_months) - timedelta(days=1)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + relativedelta(months=test_months) - timedelta(days=1)

        # Stop if test period exceeds overall end
        if test_end > end:
            # Try to fit a final window with truncated test
            if test_start <= end:
                windows.append((
                    (train_start.strftime("%Y-%m-%d"), train_end.strftime("%Y-%m-%d")),
                    (test_start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
                ))
            break

        windows.append((
            (train_start.strftime("%Y-%m-%d"), train_end.strftime("%Y-%m-%d")),
            (test_start.strftime("%Y-%m-%d"), test_end.strftime("%Y-%m-%d")),
        ))

        cursor += relativedelta(months=step_months)

    return windows


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
