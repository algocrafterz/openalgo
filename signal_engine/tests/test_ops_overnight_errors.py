"""Overnight error digest: known-benign records are counted apart and do not raise a WARN alone."""

import pytest

from signal_engine.ops import readiness
from signal_engine.ops.model import OK, WARN

DAY = "2026-11-17"
PREV = "2026-11-16"
pytestmark = pytest.mark.filterwarnings("ignore")

DROP = "Connection to remote host was lost."
DELISTED = "$NIFTYFPI.NS: possibly delisted; no price data found"
REAL = "order rejected: margin shortfall"


def _rec(message, name="x"):
    return ("16:00:00", name, message, "")


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setattr(readiness, "_previous_weekday", lambda day: PREV)

    def set_records(records):
        monkeypatch.setattr(readiness.logs, "app_errors", lambda d: iter(records if d == DAY else []))
        monkeypatch.setattr(readiness.logs, "engine_errors", lambda d: iter([]))

    return set_records


def test_empty_is_unchanged(feed):
    feed([])
    check = readiness._overnight_errors(DAY)
    assert (check.status, check.evidence) == (OK, "none")


def test_only_benign_records_are_ok_with_counts(feed):
    feed([_rec(DROP)] * 4 + [_rec(DELISTED, "yfinance")])
    check = readiness._overnight_errors(DAY)
    assert check.status == OK
    assert check.evidence == (
        "5 known-benign, 0 other; 4 websocket drops, reconnect: UNKNOWN (not in error log)"
    )


def test_mixed_is_warn_and_benign_is_not_in_top_groups(feed):
    feed([_rec(DROP)] * 6 + [_rec(REAL)] * 2)
    check = readiness._overnight_errors(DAY)
    assert check.status == WARN
    assert check.evidence.startswith("6 known-benign, 2 other; top: ")
    assert "margin shortfall" in check.evidence
    assert "remote host" not in check.evidence


def test_more_than_ten_websocket_drops_is_warn(feed):
    feed([_rec(DROP)] * 11)
    assert readiness._overnight_errors(DAY).status == WARN
    feed([_rec(DROP)] * 10)
    assert readiness._overnight_errors(DAY).status == OK


TOKEN = "reconnect: UNKNOWN"


def test_no_drops_has_no_reconnect_token(feed):
    feed([_rec(DELISTED, "yfinance")] * 3)
    check = readiness._overnight_errors(DAY)
    assert check.status == OK
    assert TOKEN not in check.evidence


def test_few_drops_are_ok_but_reconnect_is_unknown(feed):
    feed([_rec(DROP)] * 3)
    check = readiness._overnight_errors(DAY)
    assert check.status == OK
    assert check.evidence == "3 known-benign, 0 other; 3 websocket drops, reconnect: UNKNOWN (not in error log)"


def test_ten_drops_are_ok_with_token(feed):
    feed([_rec(DROP)] * 10)
    check = readiness._overnight_errors(DAY)
    assert check.status == OK
    assert TOKEN in check.evidence


def test_eleven_drops_are_warn_with_token(feed):
    feed([_rec(DROP)] * 11)
    check = readiness._overnight_errors(DAY)
    assert check.status == WARN
    assert TOKEN in check.evidence


def test_other_plus_drops_is_warn_with_token_after_top_groups(feed):
    feed([_rec(DROP)] * 2 + [_rec(REAL)])
    check = readiness._overnight_errors(DAY)
    assert check.status == WARN
    assert check.evidence.index("margin shortfall") < check.evidence.index(TOKEN)


def test_other_without_drops_has_no_token(feed):
    feed([_rec(REAL)])
    check = readiness._overnight_errors(DAY)
    assert check.status == WARN
    assert TOKEN not in check.evidence
