"""Close-day step `value_areas`: add the day's VAH / POC / VAL rows and prove the day is covered.

Owns the value-area dataset (signal_engine/features/value_area_store.py). Runs after the backfill,
because a session's profile needs that session's 1-minute bars; it reads Historify, never fetches.
"""

from __future__ import annotations

from signal_engine.features import value_area_store
from signal_engine.ops.model import FAIL, OK, WARN, Check, write_phase


def run(day: str) -> str:
    result = value_area_store.build()
    cov = value_area_store.coverage(day)
    checks = [
        Check(
            "rows added",
            WARN if result["failed"] else OK,
            f"{result['rows_written']} rows for {result['symbols']} symbols"
            + (f"; failed: {'; '.join(result['failed'][:3])}" if result["failed"] else ""),
        ),
    ]
    if cov["with_bars"] == 0:
        checks.append(Check(f"coverage {day}", FAIL, "no 1-minute bars in Historify for the day"))
    else:
        missing = cov["missing"]
        checks.append(
            Check(
                f"coverage {day}",
                OK if not missing else WARN,
                f"{cov['stored']}/{cov['with_bars']} symbols with 1-minute bars have a value area"
                + (f"; missing {missing[:8]}" if missing else ""),
            )
        )
    checks.append(
        Check(
            "dataset",
            OK,
            f"{cov['rows']} rows, {cov['first']} to {cov['latest']} ({value_area_store.METHOD})",
        )
    )
    summary = (
        "Previous-session value areas for volume- and market-profile research and backtests, "
        "stored in signal_engine/data/value_areas.db (one row per symbol per session)."
    )
    return write_phase("value-areas", day, checks, summary)
