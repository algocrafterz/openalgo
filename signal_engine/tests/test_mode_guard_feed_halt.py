"""The feed-down halt is recoverable; the mode-flip halt is not."""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import mode_guard


@pytest.fixture(autouse=True)
def _clean_guard():
    mode_guard.reset()
    yield
    mode_guard.reset()


def test_feed_halt_blocks_entries_and_clears():
    mode_guard.halt_feed("telegram down")
    assert mode_guard.is_halted()
    assert mode_guard.halt_reason() == "telegram down"

    assert mode_guard.clear_feed_halt() is True
    assert not mode_guard.is_halted()
    assert mode_guard.halt_reason() == ""


def test_clearing_when_not_feed_halted_is_a_no_op():
    assert mode_guard.clear_feed_halt() is False


def test_feed_halt_is_idempotent():
    mode_guard.halt_feed("first")
    mode_guard.halt_feed("second")
    assert mode_guard.halt_reason() == "first"


def test_clearing_the_feed_halt_never_lifts_a_sticky_halt():
    mode_guard.halt("mode flipped")
    mode_guard.halt_feed("telegram down")

    mode_guard.clear_feed_halt()

    assert mode_guard.is_halted()
    assert mode_guard.halt_reason() == "mode flipped"


def test_sticky_reason_is_reported_ahead_of_the_feed_reason():
    mode_guard.halt_feed("telegram down")
    mode_guard.halt("mode flipped")
    assert mode_guard.halt_reason() == "mode flipped"


@pytest.mark.asyncio
async def test_mode_flip_is_still_detected_while_the_feed_is_down():
    mode_guard._startup_phase = "analyze"
    mode_guard.halt_feed("telegram down")
    with patch("signal_engine.api_client.fetch_trading_mode", AsyncMock(return_value=("live", False))):
        flipped = await mode_guard.check_for_flip()

    assert flipped == "live"
    mode_guard.clear_feed_halt()
    assert mode_guard.is_halted()
