"""Two data-integrity bugs that made ANALYZE results untrustworthy, found in 2026-09-11's run.

A. P&L WAS ATTRIBUTED FROM A PORTFOLIO-LEVEL DELTA.
   _book_broker_close() computed `pnl_delta = fetch_realised_pnl() - _last_realised_pnl`,
   which is the whole ACCOUNT's realised P&L. Two positions closing in the same poll cycle
   meant the first one booked absorbed the entire delta and the second got nothing:

     13:16:53  ADANIENSOL  booked +897.40
     13:16:53  ADANIENT    booked   +0.00

   The broker's own per-symbol figure for ADANIENSOL that day was -197.20 (confirmed by the
   15:05 reconciliation). So the day summary reported a +₹897 winner that was really a
   ₹197 loser. The positionbook already carries per-symbol realised P&L - reconciliation
   reads it - so the right number was available the whole time.

B. TRACKER-DETECTED CLOSES NEVER REACHED trades.db.
   book_close() filed an in-memory TradeRecord, advanced the day counters and sent the
   Telegram message, but wrote no EXIT row. trades.db is what every performance report and
   the ledger read, so a broker SL-M fill or a no-progress exit left no audit trail - and the
   next restart's reconciliation found "no EXIT recorded", booked it AGAIN with different
   numbers, and called record_close() a second time. All four of the day's positions closed
   at 13:16/13:35 and were re-booked at 15:05.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine.models import Direction
from signal_engine.tracker import PositionTracker, TrackedPosition, _index_positionbook


def _pos(symbol="ADANIENSOL", qty=68, fill=1372.90):
    return TrackedPosition(
        symbol=symbol, strategy="BREAKINGTRADE", exchange="NSE", product="MIS",
        entry_price=fill, quantity=qty, sl=1364.43, tp=1394.60,
        direction=Direction.LONG, entry_order_id="E1", fill_price=fill,
        ever_seen_nonzero_qty=True,
    )


class TestPositionbookCarriesPerSymbolPnl:
    def test_realised_pnl_is_indexed_per_symbol(self):
        book = [{"symbol": "ADANIENSOL", "quantity": 0, "ltp": 1370.6,
                 "today_realized_pnl": -197.2}]
        assert _index_positionbook(book)["ADANIENSOL"].realised == pytest.approx(-197.2)

    def test_quantity_and_ltp_still_available(self):
        entry = _index_positionbook([{"symbol": "X", "quantity": 5, "ltp": 10.5}])["X"]
        assert (entry.quantity, entry.ltp) == (5, 10.5)

    def test_the_pnl_field_name_the_broker_uses_is_tolerated(self):
        book = [{"symbol": "X", "quantity": 0, "ltp": 1.0, "pnl": -12.5}]
        assert _index_positionbook(book)["X"].realised == pytest.approx(-12.5)

    def test_a_missing_pnl_field_reads_as_unknown_not_zero(self):
        """Zero is a real P&L and also the orphan signal - "absent" must not impersonate it."""
        assert _index_positionbook([{"symbol": "X", "quantity": 0, "ltp": 1.0}])["X"].realised is None


class TestPerSymbolPnlIsPreferred:
    @pytest.mark.asyncio
    async def test_two_closes_in_one_cycle_each_get_their_own_pnl(self):
        """The exact 2026-09-11 failure: ADANIENSOL and ADANIENT closed together."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos("ADANIENSOL", 68, 1372.90))
        tracker.register(_pos("ADANIENT", 35, 3024.70))
        book = [
            {"symbol": "ADANIENSOL", "quantity": 0, "ltp": 1370.6, "today_realized_pnl": -197.2},
            {"symbol": "ADANIENT", "quantity": 0, "ltp": 3031.2, "today_realized_pnl": 227.5},
        ]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=30.3)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()), \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()

        booked = {c.kwargs["symbol"]: c.args[0] for c in risk.record_close.call_args_list}
        assert booked["ADANIENSOL"] == pytest.approx(-197.2)
        assert booked["ADANIENT"] == pytest.approx(227.5)

    @pytest.mark.asyncio
    async def test_the_portfolio_delta_is_the_fallback_when_the_broker_gives_no_figure(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos("SBIN", 10, 800.0))
        book = [{"symbol": "SBIN", "quantity": 0, "ltp": 805.0}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=50.0)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()), \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        assert risk.record_close.call_args.args[0] == pytest.approx(50.0)


class TestCloseIsWrittenToTheAuditTrail:
    @pytest.mark.asyncio
    async def test_a_tracker_detected_close_writes_an_exit_row(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos("SBIN", 10, 800.0))
        book = [{"symbol": "SBIN", "quantity": 0, "ltp": 805.0, "today_realized_pnl": 50.0}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=50.0)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()) as save, \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        save.assert_called_once()
        assert save.call_args.kwargs["symbol"] == "SBIN"
        assert save.call_args.kwargs["pnl"] == pytest.approx(50.0)

    def test_the_writer_swallows_its_own_failures(self):
        """The position IS closed at the broker - a bookkeeping failure must not propagate
        into the close path and leave the tracker believing otherwise. The guarantee lives
        in save_tracker_exit itself, so it is tested there rather than through a patch that
        would replace the very try/except under test."""
        from signal_engine import db

        with patch("signal_engine.db._get_connection",
                   MagicMock(side_effect=RuntimeError("disk full"))):
            db.save_tracker_exit(
                strategy="ORB", symbol="SBIN", entry=800.0, sl=796.0, tp=810.0,
                quantity=10, exit_price=805.0, pnl=50.0, exit_types=["SL"],
            )  # must not raise

    @pytest.mark.asyncio
    async def test_a_tracker_detected_close_carries_the_entry_s_sig_id(self):
        """2026-09-17: BREAKOUT positions closed by tracker detection (broker SL-M beating
        the TradingView alert here) wrote sig_id=NULL, so the ledger's sig_id-keyed
        reconciliation couldn't pair the close back to its entry - it reported the entry as
        falsely still-open and the close as a phantom orphan trade. book_close() must carry
        TrackedPosition.sig_id through to save_tracker_exit()."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        pos = _pos("PFC", qty=918, fill=346.80)
        pos.sig_id = "PFC-20260917-0955"
        tracker.register(pos)
        book = [{"symbol": "PFC", "quantity": 0, "ltp": 347.55, "today_realized_pnl": -688.50}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=-688.50)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()) as save, \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        assert save.call_args.kwargs["sig_id"] == "PFC-20260917-0955"

    @pytest.mark.asyncio
    async def test_a_position_with_no_sig_id_still_closes_cleanly(self):
        """Python-sourced strategies (BREAKINGTRADE family) never carry a SigID - the field
        must default to "" and not break the close path."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        tracker.register(_pos("BIOCON", qty=100, fill=380.0))  # sig_id defaults to ""
        book = [{"symbol": "BIOCON", "quantity": 0, "ltp": 381.0, "today_realized_pnl": 10.0}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=10.0)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()) as save, \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        assert save.call_args.kwargs["sig_id"] == ""


class TestFinalLegDoesNotDoubleCountPriorPartialExits:
    """2026-09-21: the same day's Telegram DAY SUMMARY reported Net +2,093 while the OpenAlgo
    dashboard's Realised P&L showed 547.18 - two numbers for one trading day, neither of which
    was the true one (confirmed against sandbox_trades: the actual fills sum to +1,619.65).

    _book_broker_close() treated the broker's per-symbol "realised" figure as the final leg's
    OWN delta and added it on top of pos.realized_pnl (which already held the correctly-tracked
    partial legs). For BHARTIARTL that produced 859.50 (two correct partials) + 718.80 (broker
    "realised", itself not a reliable incremental figure once a position has partial-exit
    history) = 1,578.30 booked, against 1,382.40 true from the broker's own fills (63 @ 1833.80
    for the final leg, not the 1830.69 implied by treating 718.80 as that leg's delta).

    Fix: once pos.realized_pnl != 0 (partial exits already booked), derive the final leg's
    delta from price - ltp (which the broker sets to its own execution price on a full close)
    against the position's own entry price and remaining quantity - instead of trusting the
    broker's realised-pnl bookkeeping at all.
    """

    @pytest.mark.asyncio
    async def test_a_multi_leg_close_uses_ltp_not_the_broker_s_realised_delta(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        pos = _pos("BHARTIARTL", qty=180, fill=1842.10)
        from signal_engine.models import Direction as _Dir
        pos.direction = _Dir.SHORT
        pos.quantity = 63  # TP1 (54) + TP2 (63) already exited, 63 remain
        pos.realized_pnl = 859.50  # 248.40 (TP1) + 611.10 (TP2), correctly tracked by main.py
        tracker.register(pos)
        # Broker's per-symbol "realised" (718.80) is the buggy figure from that day - present
        # to prove it is no longer used once realized_pnl is non-zero. ltp=1833.80 is the real
        # closing fill price from that day's broker tradebook.
        book = [{"symbol": "BHARTIARTL", "quantity": 0, "ltp": 1833.80, "today_realized_pnl": 718.80}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=718.80)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()), \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        booked_delta = risk.record_close.call_args.args[0]
        assert booked_delta == pytest.approx(522.90)  # (1842.10 - 1833.80) * 63, not 718.80
        total_pnl = pos.realized_pnl + booked_delta
        assert total_pnl == pytest.approx(1382.40)  # true total from the broker's own fills

    @pytest.mark.asyncio
    async def test_a_single_leg_close_is_unaffected(self):
        """No partial-exit history (realized_pnl stays at its 0.0 default) - the existing
        per-symbol-realised path must still be used, matching TestPerSymbolPnlIsPreferred."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        pos = _pos("PFC", qty=874, fill=345.85)
        tracker.register(pos)
        book = [{"symbol": "PFC", "quantity": 0, "ltp": 345.75, "today_realized_pnl": 87.40}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=87.40)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()), \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        assert risk.record_close.call_args.args[0] == pytest.approx(87.40)


class TestFinalLegAuditTrailIsNotMisattributedToTheEntry:
    """2026-09-21: every tracker-detected close wrote order_id=entry_order_id and
    quantity=original_quantity, regardless of how much was actually still open or which
    order closed it. For a position with prior partial exits this makes the final leg's
    EXIT row indistinguishable from a fabricated duplicate of the entry - the ledger's
    order_id-keyed reconciliation can never find the real broker fill that closed it, and
    reports it as "a broker fill the engine never sent". Confirmed against real BHARTIARTL
    trades.db + broker tradebook data from that day: the entry order (qty=180) was reused
    for a final-leg close that actually closed only the remaining 63.
    """

    @pytest.mark.asyncio
    async def test_a_partially_exited_position_s_final_close_uses_the_remaining_qty(self):
        risk = MagicMock()
        tracker = PositionTracker(risk)
        pos = _pos("BHARTIARTL", qty=180, fill=1842.10)
        pos.quantity = 63  # two partial exits already reduced it from 180
        pos.sl_order_id = "TRAILING-SL-3"  # the live stop that will close the remainder
        tracker.register(pos)
        book = [{"symbol": "BHARTIARTL", "quantity": 0, "ltp": 1833.8, "today_realized_pnl": 522.90}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=522.90)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()), \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()) as save, \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        assert save.call_args.kwargs["quantity"] == 63
        assert save.call_args.kwargs["order_id"] == "TRAILING-SL-3"
        assert save.call_args.kwargs["order_id"] != pos.entry_order_id

    @pytest.mark.asyncio
    async def test_a_no_progress_market_exit_s_order_id_is_captured_for_the_close(self):
        """The market order placed by the no-progress path is the closing fill - it must be
        readable back off the position when check_positions() detects qty=0 next poll."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        pos = _pos("PFC", qty=874, fill=345.85)
        pos.sl_order_id = "SL-ORIGINAL"
        tracker.register(pos)
        from signal_engine.models import OrderStatus, TradeResult
        with patch("signal_engine.tracker.cancel_order", AsyncMock(return_value=True)), \
             patch("signal_engine.tracker.send_order",
                   AsyncMock(return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="MARKET-EXIT-9"))), \
             patch("signal_engine.notifier.notify_no_progress_exit", AsyncMock()):
            await tracker._no_progress_market_exit(pos, timedelta(minutes=90), 345.75, 345.85, 0.09)
        assert pos.sl_order_id == "MARKET-EXIT-9"


class TestExitTypeNamesTheRealCause:
    @pytest.mark.asyncio
    async def test_a_no_progress_exit_is_not_labelled_sl(self):
        """Every one of the day's four closes was reported as an SL hit. None was: all four
        were no-progress market exits."""
        risk = MagicMock()
        tracker = PositionTracker(risk)
        pos = _pos("SBIN", 10, 800.0)
        pos.exit_types = ["NO-PROGRESS"]
        tracker.register(pos)
        book = [{"symbol": "SBIN", "quantity": 0, "ltp": 805.0, "today_realized_pnl": 50.0}]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)), \
             patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=50.0)), \
             patch("signal_engine.notifier.notify_position_closed", AsyncMock()) as notify, \
             patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()), \
             patch.object(tracker, "_position_too_young", return_value=False), \
             patch.object(tracker, "_maybe_send_day_summary", AsyncMock()):
            await tracker.check_positions()
        assert notify.await_args.kwargs["exit_types"] == ["NO-PROGRESS"]
