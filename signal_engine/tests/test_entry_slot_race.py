"""Entries that fire on the same bar must not all pass the open-position limit.

check_exposure() reads the slot count, but record_trade() only moves it after the capital
fetch and send_order() awaits. Several signals dispatched together (the pipeline gathers a
whole bar's fires) each passed the gate before any of them was counted, so a 1-slot strategy
opened one position per signal.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from signal_engine.main import _handle_entry
from signal_engine.models import OrderStatus, TradeResult
from signal_engine.tests.test_main_characterization import _entry_signal, _ok, harness


def _one_slot(h) -> None:
    """Make the harness risk engine allow one open position per strategy.

    Mirrors RiskEngine's contract: a reservation (reserve_entry) counts against the cap until
    release_entry, and a recorded trade counts from then on.
    """
    used: dict[str, int] = {}
    reserved: dict[str, str] = {}  # token -> strategy

    def _taken(strategy):
        return used.get(strategy, 0) + sum(1 for s in reserved.values() if s == strategy)

    def _record(strategy, symbol):
        used[strategy] = used.get(strategy, 0) + 1

    h.risk.check_exposure.side_effect = lambda strategy: _taken(strategy) < 1
    h.risk.exposure_block_reason.return_value = "max open positions"
    h.risk.reserve_entry.side_effect = lambda token, strategy, symbol="": reserved.setdefault(token, strategy)
    h.risk.release_entry.side_effect = lambda token: reserved.pop(token, None)
    h.risk.record_trade.side_effect = _record


def _slow(result):
    async def _send(_order):
        await asyncio.sleep(0.01)  # the broker round trip other entries used to slip through
        return result

    return _send


class TestSameBarEntries:
    @pytest.mark.asyncio
    async def test_only_one_entry_takes_the_last_slot(self):
        h = harness(send_order=AsyncMock(side_effect=_slow(_ok("E1"))))
        _one_slot(h)
        with h.stack:
            await asyncio.gather(
                _handle_entry(_entry_signal(symbol="RELIANCE")),
                _handle_entry(_entry_signal(symbol="INFY")),
                _handle_entry(_entry_signal(symbol="TCS")),
            )
        assert h.send_order.await_count == 1

    @pytest.mark.asyncio
    async def test_other_strategies_are_not_held_up(self):
        """The slot is per strategy, so a different strategy's entry still goes out."""
        h = harness(send_order=AsyncMock(side_effect=_slow(_ok("E1"))))
        _one_slot(h)
        with h.stack:
            await asyncio.gather(
                _handle_entry(_entry_signal(symbol="RELIANCE", strategy="ORB")),
                _handle_entry(_entry_signal(symbol="INFY", strategy="BREAKOUT")),
            )
        assert h.send_order.await_count == 2

    @pytest.mark.asyncio
    async def test_a_signal_during_an_in_flight_entry_is_declined_not_queued(self):
        """The reservation holds the slot while the first order is in flight; a concurrent
        second signal is declined, even if the first order is then rejected (it is not queued
        to be sent late)."""
        rejected = TradeResult(status=OrderStatus.REJECTED, message="margin")
        h = harness(send_order=AsyncMock(side_effect=_slow(rejected)))
        _one_slot(h)
        with h.stack:
            await asyncio.gather(
                _handle_entry(_entry_signal(symbol="RELIANCE")),
                _handle_entry(_entry_signal(symbol="INFY")),
            )
        assert h.send_order.await_count == 1
        h.risk.record_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_rejected_entry_frees_the_slot_for_a_later_signal(self):
        rejected = TradeResult(status=OrderStatus.REJECTED, message="margin")
        h = harness(send_order=AsyncMock(side_effect=[rejected, _ok("E2")]))
        _one_slot(h)
        with h.stack:
            await _handle_entry(_entry_signal(symbol="RELIANCE"))
            await _handle_entry(_entry_signal(symbol="INFY"))
        assert h.send_order.await_count == 2
        h.risk.record_trade.assert_called_once()
