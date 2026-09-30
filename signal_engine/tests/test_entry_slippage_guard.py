"""Entry-fill slippage guard.

TCS 2026-09-30: signal entry 2085.90 / SL 2079.74 / TP1 2092.06 (R = 6.16), market fill
2091.00. The fill consumed 0.83R of a 1R target, so "TP1 HIT" later booked a loss and the
runner stop (anchored to the signal price) sat below the real cost. A fill whose adverse
slippage exceeds max_entry_slippage_r is now closed immediately, like a fill past TP.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine.main import _auto_close_on_bad_fill, entry_slippage_r
from signal_engine.models import Direction, Signal


def _signal(direction=Direction.LONG, entry=2085.9, sl=2079.74, tp=2092.06) -> Signal:
    return Signal(
        strategy="BREAKOUT",
        direction=direction,
        symbol="TCS",
        entry=entry,
        sl=sl,
        tp=tp,
        raw_message="x",
    )


class TestEntrySlippageR:
    def test_tcs_long_fill_above_signal_is_adverse(self):
        assert entry_slippage_r(_signal(), 2091.0) == pytest.approx(5.1 / 6.16)

    def test_long_fill_below_signal_is_favourable(self):
        assert entry_slippage_r(_signal(), 2085.1) < 0

    def test_short_fill_below_signal_is_adverse(self):
        sig = _signal(Direction.SHORT, entry=892.1, sl=894.78, tp=889.42)
        assert entry_slippage_r(sig, 890.76) == pytest.approx(0.5)

    def test_short_fill_above_signal_is_favourable(self):
        sig = _signal(Direction.SHORT, entry=892.1, sl=894.78, tp=889.42)
        assert entry_slippage_r(sig, 892.9) < 0

    def test_zero_risk_distance_returns_zero(self):
        assert entry_slippage_r(_signal(sl=2085.9), 2091.0) == 0.0

    def test_missing_fill_returns_zero(self):
        assert entry_slippage_r(_signal(), None) == 0.0


async def _run_guard(signal, fill, max_slip_r=0.5):
    settings = SimpleNamespace(exchange="NSE", product="MIS", max_entry_slippage_r=max_slip_r)
    send_order = AsyncMock()
    cancel = AsyncMock()
    notifier = MagicMock()
    notifier.notify_order_rejected = AsyncMock()
    risk = MagicMock()
    with (
        patch("signal_engine.main.settings", settings),
        patch("signal_engine.main.send_order", send_order),
        patch("signal_engine.main.cancel_order", cancel),
        patch("signal_engine.main.notifier", notifier),
        patch("signal_engine.main.risk_engine", risk),
    ):
        closed = await _auto_close_on_bad_fill(signal, 147, fill, "SL1")
    return closed, send_order, cancel, notifier, risk


@pytest.mark.asyncio
class TestAutoCloseOnBadFill:
    async def test_excess_slippage_closes_and_cancels_sl(self):
        closed, send_order, cancel, notifier, risk = await _run_guard(_signal(), 2091.0)
        assert closed is True
        cancel.assert_awaited_once_with("SL1", "BREAKOUT")
        send_order.assert_awaited_once()
        notifier.notify_order_rejected.assert_awaited_once()
        risk.record_close.assert_called_once()

    async def test_moderate_slippage_keeps_position(self):
        closed, send_order, cancel, _, _ = await _run_guard(_signal(), 2087.0)
        assert closed is False
        send_order.assert_not_awaited()
        cancel.assert_not_awaited()

    async def test_fill_past_tp_still_closes(self):
        closed, send_order, _, _, _ = await _run_guard(_signal(), 2093.0, max_slip_r=99.0)
        assert closed is True
        send_order.assert_awaited_once()

    async def test_guard_disabled_when_threshold_zero(self):
        closed, send_order, _, _, _ = await _run_guard(_signal(), 2091.0, max_slip_r=0.0)
        assert closed is False
        send_order.assert_not_awaited()

    async def test_missing_fill_keeps_position(self):
        closed, send_order, _, _, _ = await _run_guard(_signal(), None)
        assert closed is False
        send_order.assert_not_awaited()
