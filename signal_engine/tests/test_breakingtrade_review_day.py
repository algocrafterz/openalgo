"""--review reads the day it is given (BUG-009: a past-day eod.sh re-run reviewed the run date)."""

import sys
from datetime import date
from unittest.mock import patch

from signal_engine.analysis.breakingtrade import __main__ as cli


def _run(argv):
    with (
        patch.object(sys, "argv", ["breakingtrade", *argv]),
        patch("signal_engine.analysis.breakingtrade.review.report") as report,
    ):
        cli.main()
    return report


def test_review_reads_the_requested_day():
    assert _run(["--review", "--day", "2026-10-30"]).call_args.args == (date(2026, 10, 30),)


def test_review_without_a_day_keeps_today():
    assert _run(["--review"]).call_args.args == (None,)
