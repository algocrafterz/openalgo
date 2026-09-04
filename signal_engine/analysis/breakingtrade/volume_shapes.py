"""The Volume Scanner's "row shapes", from the vendor's own pattern table.

Source: BreakingTrade_Full_Reference.docx, "Live Volume Scanner - Row shapes (pattern
recognition)". Each function below is one row of that table, expressed against the per-session
volume factors the extractor keeps as vol_a ... vol_m:

    Staircase      several consecutive elevated sessions, nothing extreme
                   -> a large order worked over time (institutional accumulation)
                   -> "Most tradeable pattern - check Del% for genuine ownership vs churn"
    Spike          one violent cell, ordinary either side -> news or a block deal
                   -> "follow-through staying elevated = repricing, snap-back = one-off"
    Lunch Anomaly  green in G/H/I (12:15-13:45) -> size traded while the market is quiet
                   -> "most reliably front-runs an afternoon move"
    Closing Ramp   volume building into K/L/M with price holding -> overnight ownership
                   -> "watchlist for tomorrow's open more than a same-day trade"
    Ghost Rally    price up 1.5%+ on a row of below-average sessions -> thin book
                   -> "do not chase - one real seller unwinds it fast"

THE 'O' BUCKET IS EXCLUDED FROM EVERY SHAPE. It covers only 09:15-09:20, and the guide is
explicit: "First 5 minutes (9:15-9:20) only - sits inside session A, don't double-count."
Its factor is computed over a five-minute window so it runs an order of magnitude hotter than
the half-hour sessions (118x is a normal-looking O reading) and would swamp any comparison.

An unfinished session reads NaN, not 0 - "hasn't traded yet" and "traded nothing" are
different, and conflating them would make every morning look like a Ghost Rally.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.analysis.breakingtrade.scans import (
    VOLUME_ELEVATED,
    VOLUME_EXTREME,
    VOLUME_NORMAL,
)

# Half-hour TPO sessions in order. 'o' is deliberately absent - see the module docstring.
SESSION_LETTERS = ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m")
LUNCH_LETTERS = ("g", "h", "i")  # 12:15-13:45
CLOSING_LETTERS = ("k", "l", "m")  # 14:15-15:30
LATE_LETTERS = ("l", "m")  # 14:45-15:30 - where a closing ramp lives if you can wait for it

# What is actually READABLE in time to place a delivery order. From 2026-08-03 NSE runs a
# Closing Auction Session, and for F&O stocks - our entire universe - continuous trading ends
# at 15:15. L only completes AT 15:15 and M is the auction itself, so a decision needing either
# cannot be acted on in the continuous market. K (14:15-14:45) is complete at 14:45, leaving
# 25 minutes to act. It is also reported for 100% of names, against 74% for L and M.
EXECUTABLE_LETTERS = ("k",)

GHOST_RALLY_MIN_CHANGE_PCT = 1.5  # "price up 1.5%+, every session red"
STAIRCASE_MIN_RUN = 3  # "several consecutive elevated sessions"


def session_factors(row, letters=SESSION_LETTERS) -> list:
    """[(letter, factor)] for sessions that have actually traded, in session order."""
    factors = []
    for letter in letters:
        value = row.get(f"vol_{letter}")
        if value is not None and pd.notna(value):
            factors.append((letter, float(value)))
    return factors


def is_staircase(row) -> bool:
    """A run of consecutive elevated sessions with nothing extreme in it."""
    run = 0
    for _, factor in session_factors(row):
        if VOLUME_ELEVATED <= factor < VOLUME_EXTREME:
            run += 1
            if run >= STAIRCASE_MIN_RUN:
                return True
        else:
            run = 0
    return False


def is_spike(row) -> bool:
    """One extreme session with ordinary sessions on whichever sides exist."""
    factors = session_factors(row)
    for index, (_, factor) in enumerate(factors):
        if factor < VOLUME_EXTREME:
            continue
        neighbours = [
            value
            for position, (_, value) in enumerate(factors)
            if position in (index - 1, index + 1)
        ]
        if neighbours and all(value < VOLUME_ELEVATED for value in neighbours):
            return True
    return False


def is_lunch_anomaly(row) -> bool:
    """Deliberate size traded through the quiet middle of the session."""
    lunch = session_factors(row, LUNCH_LETTERS)
    return bool(lunch) and any(factor >= VOLUME_ELEVATED for _, factor in lunch)


def is_closing_ramp(row, letters=LATE_LETTERS) -> bool:
    """Volume building into the close while price holds - someone wants it overnight.

    "Building" is read as the closing block being BUSY, not as a monotonically rising
    staircase. Requiring each session to beat the previous one was tested against a real
    session and threw away obvious candidates on a single dip: DELHIVERY closed +2.1% on
    K 1.20 -> L 3.98 -> M 1.99, unmistakably heavy late participation, yet failed only because
    M came in under L. Half-hour volume is far too noisy for a monotonic test to carry meaning.

    So: at least one of the last two sessions is elevated, and the pair is not thin overall.
    """
    late = [factor for _, factor in session_factors(row, letters)]
    if not late:
        return False

    busy = max(late) >= VOLUME_ELEVATED
    not_thin = (sum(late) / len(late)) >= VOLUME_NORMAL

    change = row.get("change_pct")
    holding = pd.notna(change) and float(change) >= 0
    return busy and not_thin and holding


def is_ghost_rally(row) -> bool:
    """A price move with no participation behind it anywhere in the day."""
    change = row.get("change_pct")
    if pd.isna(change) or float(change) < GHOST_RALLY_MIN_CHANGE_PCT:
        return False
    factors = session_factors(row)
    return bool(factors) and all(factor < VOLUME_NORMAL for _, factor in factors)


SHAPES = {
    "staircase": is_staircase,
    "spike": is_spike,
    "lunch_anomaly": is_lunch_anomaly,
    "closing_ramp": is_closing_ramp,
    "closing_ramp_executable": lambda row: is_closing_ramp(row, EXECUTABLE_LETTERS),
    "ghost_rally": is_ghost_rally,
}


def label_shapes(volume: pd.DataFrame) -> pd.DataFrame:
    """Add one boolean column per row shape to a normalized volume frame."""
    labelled = volume.copy()
    for name, detector in SHAPES.items():
        labelled[name] = labelled.apply(detector, axis=1)
    return labelled
