"""C2 (enforcement): a halted engine must refuse new entries and say why.

mode_guard.halt() is set by a mid-session mode flip (mode_guard.check_for_flip) and by the
listener exhausting its reconnect budget (startup._enter_degraded_mode). Both mean the same
thing for an entry: the conditions this trade would be sized and managed under are no longer
the ones the engine is configured for.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import main, mode_guard
from signal_engine.tests.conftest import make_signal as _make_signal


@pytest.fixture(autouse=True)
def _clean_guard():
    mode_guard.reset()
    yield
    mode_guard.reset()


class TestHaltBlocksEntries:
    @pytest.mark.asyncio
    async def test_halted_engine_refuses_an_entry(self):
        mode_guard.halt("mode flipped to LIVE mid-session")
        with patch.object(main, "_decline", AsyncMock()) as decline, \
             patch.object(main.notifier, "notify_risk_limit_hit", AsyncMock()), \
             patch.object(main, "_entry_rejected_by_symbol_rules", AsyncMock(return_value=False)), \
             patch.object(main, "send_order", AsyncMock()) as send:
            await main._handle_entry(_make_signal())
        send.assert_not_awaited()
        decline.assert_awaited_once()
        assert decline.await_args.kwargs["stage"] == "halted"

    @pytest.mark.asyncio
    async def test_the_halt_reason_reaches_telegram(self):
        mode_guard.halt("listener exhausted its reconnect budget")
        with patch.object(main, "_decline", AsyncMock()), \
             patch.object(main.notifier, "notify_risk_limit_hit", AsyncMock()) as notify, \
             patch.object(main, "_entry_rejected_by_symbol_rules", AsyncMock(return_value=False)), \
             patch.object(main, "send_order", AsyncMock()):
            await main._handle_entry(_make_signal())
        notify.assert_awaited_once()
        assert "reconnect budget" in notify.await_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unhalted_engine_proceeds_past_the_gate(self):
        with patch.object(main, "_entry_rejected_by_symbol_rules", AsyncMock(return_value=True)) as sym:
            await main._handle_entry(_make_signal())
        sym.assert_awaited_once()  # reached the next gate rather than short-circuiting


class TestExitsAreNeverHalted:
    @pytest.mark.asyncio
    async def test_halt_does_not_block_closing_a_position(self):
        """A halt stops NEW risk. Refusing to close an open position would strand it."""
        mode_guard.halt("mode flipped")
        with patch.object(main, "tracker") as tr, \
             patch.object(main.notifier, "notify_exit_no_position", AsyncMock()):
            tr._time_exit_active = False
            tr.find_position.return_value = None
            signal = _make_signal(direction=main.Direction.EXIT, tp_level="TP1")
            await main._handle_exit(signal)
        tr.find_position.assert_called()
