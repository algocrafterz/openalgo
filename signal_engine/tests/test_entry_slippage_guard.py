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
from signal_engine.models import Direction, OrderStatus, Signal, TradeResult


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
    send_order = AsyncMock(return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="C1"))
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
        patch("signal_engine.main.fetch_order_fill_price", AsyncMock(return_value=None)),
        patch("signal_engine.main._book_realised_pnl_delta", AsyncMock(return_value=(0.0, 0.0))),
    ):
        outcome = await _auto_close_on_bad_fill(signal, 147, fill, "SL1")
    closed = outcome is not None and outcome.closed
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


async def _run_guard_with_stop(stop_status, stop_fill=None):
    """A bad fill whose stop refuses the cancel; returns (outcome, send_order, notifier, risk)."""
    settings = SimpleNamespace(exchange="NSE", product="MIS", max_entry_slippage_r=0.5)
    send_order = AsyncMock(return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="C1"))
    notifier = MagicMock()
    notifier.notify_order_rejected = AsyncMock()
    notifier.notify_exit_failed = AsyncMock()
    risk = MagicMock()
    with (
        patch("signal_engine.main.settings", settings),
        patch("signal_engine.main.send_order", send_order),
        patch("signal_engine.main.cancel_order", AsyncMock(return_value=False)),
        patch("signal_engine.main.fetch_order_status", AsyncMock(return_value=stop_status)),
        patch("signal_engine.main.notifier", notifier),
        patch("signal_engine.main.risk_engine", risk),
        patch("signal_engine.main.fetch_order_fill_price", AsyncMock(return_value=stop_fill)),
        patch("signal_engine.main._book_realised_pnl_delta", AsyncMock(return_value=(0.0, 0.0))),
    ):
        outcome = await _auto_close_on_bad_fill(_signal(), 147, 2091.0, "SL1")
    return outcome, send_order, notifier, risk


@pytest.mark.asyncio
class TestBadFillStopCancelFails:
    """The stop could not be cancelled: the close must not sell into it or past it."""

    async def test_filled_stop_is_booked_as_the_close_and_nothing_is_sent(self):
        outcome, send_order, _, risk = await _run_guard_with_stop("complete", stop_fill=2079.0)
        send_order.assert_not_awaited()
        assert outcome.closed is True
        assert outcome.close_order_id == "SL1"
        assert outcome.exit_fill_price == 2079.0
        assert outcome.pnl == pytest.approx((2079.0 - 2091.0) * 147)
        risk.record_close.assert_called_once()

    async def test_working_stop_keeps_the_position_tracked_under_it(self):
        outcome, send_order, notifier, _ = await _run_guard_with_stop("trigger pending")
        send_order.assert_not_awaited()
        assert outcome.closed is False
        assert outcome.replacement_sl_order_id == "SL1"
        notifier.notify_exit_failed.assert_awaited_once()

    async def test_stop_already_cancelled_still_closes(self):
        outcome, send_order, _, _ = await _run_guard_with_stop("cancelled")
        send_order.assert_awaited_once()
        assert outcome.closed is True


class TestStopBreachedFill:
    """Owner decision 2026-10-08: a long filled at or below its stop is a bad fill of its own kind."""

    @pytest.mark.asyncio
    async def test_the_guard_closes_a_fill_through_the_stop_and_names_it(self):
        from signal_engine import main as main_mod

        sig = _signal()
        verdict = main_mod.trade_policy.bad_fill(1, sig.entry, sig.sl, sig.tp, sig.sl - 0.5, 0.5)
        assert verdict.violated and verdict.kind == main_mod.trade_policy.STOP_BREACHED
        assert main_mod.EXIT_ENTRY_REJECTED_STOP_BREACHED == "ENTRY_REJECTED_STOP_BREACHED"


# --------------------------------------------------------------------------
# Bad-fill close that times out: only a definite copy is adopted (owner rule 2026-10-26)
# --------------------------------------------------------------------------


def _close_row(stamp, order_id="C9"):
    return {"orderid": order_id, "symbol": "TCS", "action": "SELL", "quantity": 147,
            "order_status": "complete", "timestamp": stamp, "strategy": "BREAKOUT"}


async def _run_close_timeout(book, own_qty):
    """A bad fill whose close order times out; the order book and broker position are as given."""
    from signal_engine import order_reconcile

    order_reconcile.clear_uncertain("BREAKOUT", "TCS")
    settings = SimpleNamespace(exchange="NSE", product="MIS", max_entry_slippage_r=0.5)
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    notifier.notify_order_rejected = AsyncMock()
    notifier.notify_exit_failed = AsyncMock()
    risk = MagicMock()
    tracker = MagicMock()
    tracker.fetch_own_position = AsyncMock(return_value=own_qty)
    bracket = AsyncMock(return_value="SL-NEW")
    timeout = TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
    with (
        patch("signal_engine.main.settings", settings),
        patch("signal_engine.main.send_order", AsyncMock(return_value=timeout)),
        patch("signal_engine.main.cancel_order", AsyncMock(return_value=True)),
        patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=book)),
        patch("signal_engine.main.fetch_order_ids_today", MagicMock(return_value=set())),
        patch("signal_engine.main.tracker", tracker),
        patch("signal_engine.main.notifier", notifier),
        patch("signal_engine.main.risk_engine", risk),
        patch("signal_engine.main._place_entry_bracket", bracket),
        patch("signal_engine.main.fetch_order_fill_price", AsyncMock(return_value=None)),
        patch("signal_engine.main._book_realised_pnl_delta", AsyncMock(return_value=(0.0, 0.0))),
    ):
        outcome = await _auto_close_on_bad_fill(_signal(), 147, 2091.0, "SL1", "E1")
    held = order_reconcile.get_uncertain("BREAKOUT", "TCS")
    order_reconcile.clear_uncertain("BREAKOUT", "TCS")
    return outcome, bracket, notifier, risk, held


def _now_stamp(seconds=0.0):
    from datetime import datetime, timedelta

    from signal_engine.timeutils import IST

    return (datetime.now(IST) + timedelta(seconds=seconds)).strftime("%H:%M:%S %d-%m-%Y")


@pytest.mark.asyncio
class TestBadFillCloseTimeout:
    async def test_an_ambiguous_copy_with_the_position_open_is_held_not_reprotected(self):
        outcome, bracket, notifier, _, held = await _run_close_timeout(
            [_close_row("not a time")], own_qty=147
        )
        assert outcome.closed is False and outcome.replacement_sl_order_id == ""
        bracket.assert_not_awaited()  # a stop beside a close that may land would reverse it
        assert held is not None and held.kind == "EXIT" and held.quantity == 147
        alert = " ".join(str(c.args[0]) for c in notifier.notify.await_args_list)
        assert "UNPROTECTED" in alert and "C9" in alert

    async def test_an_ambiguous_copy_with_the_broker_unreachable_is_held(self):
        outcome, bracket, _, _, held = await _run_close_timeout([_close_row("not a time")], own_qty=None)
        assert outcome.closed is False and held is not None
        bracket.assert_not_awaited()

    async def test_an_ambiguous_copy_with_the_broker_flat_is_booked_closed(self):
        outcome, bracket, _, risk, held = await _run_close_timeout([_close_row("not a time")], own_qty=0)
        assert outcome.closed is True and held is None
        risk.record_close.assert_called_once()
        bracket.assert_not_awaited()

    async def test_a_definite_flattrade_copy_is_adopted(self):
        outcome, _, _, _, held = await _run_close_timeout([_close_row(_now_stamp(2))], own_qty=0)
        assert outcome.closed is True and outcome.close_order_id == "C9" and held is None

    async def test_no_copy_with_the_position_open_restores_the_stop(self):
        outcome, bracket, _, _, held = await _run_close_timeout([], own_qty=147)
        assert outcome.closed is False and held is None
        bracket.assert_awaited_once()  # unchanged: a proven-absent close gets its stop back
