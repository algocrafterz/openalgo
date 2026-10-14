"""Shared time helpers.

The signal engine is India-market-only: every timestamp it reasons about — signal
arrival, position age, square-off scheduling, daily counter rollover — is in IST.
IST itself is defined once in utils.ist (shared with the broker adapters, which
cannot import from signal_engine); this module re-exports it for signal_engine's
internal call sites, which historically redefined it independently across five
modules.
"""

from datetime import date, datetime

from utils.ist import IST

__all__ = ["IST", "ist_today"]


def ist_today() -> date:
    """Today's date in IST, independent of the host timezone (unlike date.today())."""
    return datetime.now(IST).date()
