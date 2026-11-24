"""The failsafe square-off must report what it really did.

It used to discard the result of every cancel and close call, log "Squareoff: done" and exit 0,
so a refused close at the safety net looked identical to a flat book.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from signal_engine.scripts import openalgoscheduler as sched


@pytest.fixture
def squareoff(monkeypatch, caplog):
    """Patch every collaborator of _run_squareoff; return the mocks."""
    from signal_engine import api_client, config

    mocks = SimpleNamespace(
        cancel=AsyncMock(return_value=True),
        close=AsyncMock(return_value=True),
        book=AsyncMock(return_value=[]),
        # The CRITICAL lines the run logged: the only place a failed square-off is reported now.
        critical=lambda: [  # noqa: E731
            r.getMessage() for r in caplog.records if r.levelno >= logging.CRITICAL
        ],
    )
    monkeypatch.setattr(api_client, "cancel_all_orders", mocks.cancel)
    monkeypatch.setattr(api_client, "close_all_positions", mocks.close)
    monkeypatch.setattr(api_client, "fetch_positionbook", mocks.book)
    monkeypatch.setattr(
        config,
        "settings",
        SimpleNamespace(
            strategy_profiles={"ORB": {"product": "MIS"}, "EMA9": {"product": "MIS"},
                               "SWING": {"product": "CNC"}}
        ),
    )
    monkeypatch.setattr(sched, "_SQUAREOFF_RETRY_DELAY_S", 0.0, raising=False)
    caplog.set_level(logging.DEBUG)
    return mocks


def _run_expecting_exit_code(code):
    with pytest.raises(SystemExit) as info:
        sched._run_squareoff()
    assert info.value.code == code


def test_everything_succeeds_and_book_is_flat_exits_zero(squareoff):
    sched._run_squareoff()  # returns normally, so the process exits 0

    assert squareoff.cancel.await_count == 2
    assert squareoff.close.await_count == 2
    assert squareoff.book.await_count == 1
    assert squareoff.critical() == []


def test_a_flat_book_with_only_non_mis_rows_is_flat(squareoff):
    squareoff.book.return_value = [
        {"symbol": "TCS", "product": "CNC", "quantity": "10"},
        {"symbol": "INFY", "product": "MIS", "quantity": "0"},
    ]

    sched._run_squareoff()

    assert squareoff.critical() == []


def test_a_refused_close_is_retried_then_logged_critical_and_exits_non_zero(squareoff):
    squareoff.close.return_value = False
    squareoff.book.return_value = [{"symbol": "INFY", "product": "MIS", "quantity": "-50"}]

    _run_expecting_exit_code(1)

    # 2 strategies x (1 attempt + 2 retries)
    assert squareoff.close.await_count == 6
    assert len(squareoff.critical()) == 1
    assert "INFY" in squareoff.critical()[0]
    assert "position book" in squareoff.critical()[0].lower()


def test_a_close_that_succeeds_on_the_retry_is_not_a_failure(squareoff):
    squareoff.close.side_effect = [False, True, True]
    still_open = [{"symbol": "INFY", "product": "MIS", "quantity": "-50"}]
    squareoff.book.side_effect = [still_open, []]  # before the retry, then the final check

    sched._run_squareoff()

    assert squareoff.close.await_count == 3
    assert squareoff.critical() == []


def test_a_failed_close_is_not_resent_when_the_book_is_already_flat(squareoff):
    """The close is account-wide. A request that timed out may have done its work, and a second
    one sent on top of it can close a position twice."""
    squareoff.close.return_value = False  # every close call reports failure
    squareoff.book.return_value = []  # but the book is flat

    sched._run_squareoff()

    assert squareoff.close.await_count == 2  # one per strategy, no retry
    assert squareoff.critical() == []


def test_a_failed_close_is_not_resent_blind_when_the_book_cannot_be_read(squareoff):
    squareoff.close.return_value = False
    squareoff.book.return_value = None

    _run_expecting_exit_code(1)

    assert squareoff.close.await_count == 2  # one per strategy, no retry
    assert len(squareoff.critical()) == 1


def test_no_retry_starts_after_the_deadline(squareoff, monkeypatch):
    monkeypatch.setattr(sched, "_SQUAREOFF_RETRY_DEADLINE_S", -1.0)
    squareoff.close.return_value = False
    squareoff.book.return_value = [{"symbol": "INFY", "product": "MIS", "quantity": "-50"}]

    _run_expecting_exit_code(1)

    assert squareoff.close.await_count == 2
    assert len(squareoff.critical()) == 1


def test_an_exception_in_the_first_strategy_does_not_skip_the_second(squareoff):
    squareoff.cancel.side_effect = [RuntimeError("boom"), True, True, True]
    squareoff.close.side_effect = [RuntimeError("boom"), RuntimeError("boom"),
                                   RuntimeError("boom"), True]

    _run_expecting_exit_code(1)

    strategies_closed = {call.args[0] for call in squareoff.close.await_args_list}
    strategies_cancelled = {call.args[0] for call in squareoff.cancel.await_args_list}
    assert strategies_closed == {"ORB", "EMA9"}
    assert strategies_cancelled == {"ORB", "EMA9"}
    assert len(squareoff.critical()) == 1


def test_an_unreadable_position_book_is_a_failure(squareoff):
    squareoff.book.return_value = None

    _run_expecting_exit_code(1)

    assert len(squareoff.critical()) == 1
    assert "position book unavailable" in squareoff.critical()[0]


def test_a_position_still_open_after_a_successful_close_is_a_failure(squareoff):
    squareoff.book.return_value = [
        {"symbol": "RELIANCE", "product": "MIS", "quantity": 25},
        {"symbol": "TCS", "product": "CNC", "quantity": 5},
    ]

    _run_expecting_exit_code(1)

    assert len(squareoff.critical()) == 1
    assert "RELIANCE" in squareoff.critical()[0]
    assert "TCS" not in squareoff.critical()[0]


def test_the_failure_is_one_critical_line_with_no_routine_notice(squareoff):
    squareoff.book.return_value = None

    _run_expecting_exit_code(1)

    assert len(squareoff.critical()) == 1


def test_the_critical_line_is_for_a_trader(squareoff):
    squareoff.book.return_value = None

    _run_expecting_exit_code(1)

    text = squareoff.critical()[0]
    assert "Check the position book now" in text
    for banned in ("Traceback", "Exception", "HTTP", "paper trading", "armed"):
        assert banned.lower() not in text.lower()
