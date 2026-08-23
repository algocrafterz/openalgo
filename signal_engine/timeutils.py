"""Shared time helpers.

The signal engine is India-market-only: every timestamp it reasons about — signal
arrival, position age, square-off scheduling, daily counter rollover — is in IST.
Defining the zone once here keeps those decisions consistent; it was previously
redefined independently in five modules.
"""

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Current wall-clock time in IST."""
    return datetime.now(IST)
