"""C2: one source of truth for the running mode, and a halt when it flips mid-session.

The mode used to be resolved ONCE, in startup._run_engine(), and fanned out to
apply_trade_mode / db.set_trade_mode / logger_setup.set_mode. Nothing re-checked it, while
notifier and the BreakingTrade poller both re-checked every 60s. A flip mid-session left the
engine placing REAL orders while still holding the analyze profile — max_open_positions 0
(unlimited) and all three loss limits at 1.0 (off) — and stamping trades.db, risk.db and the
log file "analyze". Real money, every risk limit disabled, audit trail labelled paper.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import mode_guard


@pytest.fixture(autouse=True)
def _clean_guard():
    mode_guard.reset()
    yield
    mode_guard.reset()


def _fetch(mode: str):
    is_analyze = mode == "analyze"
    return AsyncMock(return_value=(mode, is_analyze))


class TestCurrentPhase:
    @pytest.mark.asyncio
    async def test_reads_openalgo_and_caches(self):
        fetch = _fetch("live")
        with patch("signal_engine.api_client.fetch_trading_mode", fetch):
            assert await mode_guard.current_phase() == "live"
            assert await mode_guard.current_phase() == "live"
        assert fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_unknown_mode_falls_back_to_the_last_known_phase(self):
        mode_guard.prime("live")
        with patch("signal_engine.api_client.fetch_trading_mode", _fetch("unknown")):
            mode_guard._cache["checked_at"] = 0.0
            assert await mode_guard.current_phase() == "live"

    @pytest.mark.asyncio
    async def test_unknown_with_no_prior_knowledge_defaults_to_analyze(self):
        with patch("signal_engine.api_client.fetch_trading_mode", _fetch("unknown")):
            assert await mode_guard.current_phase() == "analyze"


class TestFlipDetection:
    @pytest.mark.asyncio
    async def test_no_flip_when_the_mode_is_unchanged(self):
        mode_guard.set_startup_phase("analyze")
        mode_guard.prime("analyze")
        with patch("signal_engine.api_client.fetch_trading_mode", _fetch("analyze")):
            mode_guard._cache["checked_at"] = 0.0
            assert await mode_guard.check_for_flip() is None
        assert mode_guard.is_halted() is False

    @pytest.mark.asyncio
    async def test_flip_halts_and_reports_the_new_phase(self):
        mode_guard.set_startup_phase("analyze")
        mode_guard.prime("analyze")
        with patch("signal_engine.api_client.fetch_trading_mode", _fetch("live")):
            mode_guard._cache["checked_at"] = 0.0
            assert await mode_guard.check_for_flip() == "live"
        assert mode_guard.is_halted() is True
        reason = mode_guard.halt_reason().lower()
        assert "analyze" in reason and "live" in reason

    @pytest.mark.asyncio
    async def test_flip_is_reported_once_not_on_every_poll(self):
        mode_guard.set_startup_phase("analyze")
        mode_guard.prime("analyze")
        with patch("signal_engine.api_client.fetch_trading_mode", _fetch("live")):
            mode_guard._cache["checked_at"] = 0.0
            assert await mode_guard.check_for_flip() == "live"
            mode_guard._cache["checked_at"] = 0.0
            assert await mode_guard.check_for_flip() is None
        assert mode_guard.is_halted() is True

    @pytest.mark.asyncio
    async def test_unreachable_openalgo_is_not_a_flip(self):
        """A failed check must never be mistaken for a mode change — that would halt the
        engine on a transient network blip."""
        mode_guard.set_startup_phase("live")
        mode_guard.prime("live")
        with patch("signal_engine.api_client.fetch_trading_mode", _fetch("unknown")):
            mode_guard._cache["checked_at"] = 0.0
            assert await mode_guard.check_for_flip() is None
        assert mode_guard.is_halted() is False

    @pytest.mark.asyncio
    async def test_no_startup_phase_means_nothing_to_compare(self):
        with patch("signal_engine.api_client.fetch_trading_mode", _fetch("live")):
            assert await mode_guard.check_for_flip() is None
        assert mode_guard.is_halted() is False


class TestHaltState:
    def test_starts_unhalted(self):
        assert mode_guard.is_halted() is False
        assert mode_guard.halt_reason() == ""

    def test_halt_is_sticky(self):
        mode_guard.halt("something happened")
        assert mode_guard.is_halted() is True
        mode_guard.halt("something else")
        assert mode_guard.halt_reason() == "something happened"
