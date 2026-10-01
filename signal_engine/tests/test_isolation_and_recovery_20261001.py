"""2026-10-01 incidents: stale-order recovery, cross-strategy position bleed, SL timeout dupes.

ADANIPORTS (BREAKOUT-PY, 10:50 and 11:20): the 11:20 re-entry timed out; the recovery matcher
adopted the 10:50 entry order (same symbol, SELL, 171, complete) instead of the new fill.
TCS (Pine BREAKOUT vs BREAKOUT-PY): Pine's late TP1 alert hit the restart-fallback, which read the
broker's NET TCS position (all of it BREAKOUT-PY's) and sold 32 of PY's shares.
ADANIPORTS stops: five client-side SL timeouts were each retried; all five reached the sandbox
once it unfroze, leaving a net long of 684.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import executor
from signal_engine.main import _handle_entry, _handle_exit
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.test_main_characterization import (
    _entry_signal,
    _exit_signal,
    _position,
    _real_tracker,
    _settings_stub,
    harness,
)

TIMEOUT = TradeResult(order_id="", status=OrderStatus.TIMEOUT, message="Request timed out")


def _entry_order(order_id, strategy="BREAKOUT-PY", **extra):
    return {
        "symbol": "RELIANCE",
        "action": "SELL",
        "quantity": 1,
        "order_status": "complete",
        "orderid": order_id,
        "strategy": strategy,
        **extra,
    }


def _timeout_harness(orderbook, known_ids=()):
    tracker = _real_tracker()
    h = harness(
        tracker=tracker,
        settings=_settings_stub(
            entry_timeout_recovery_attempts=1, entry_timeout_recovery_delay_seconds=0
        ),
        send_order=AsyncMock(return_value=TIMEOUT),
        fetch_orderbook=AsyncMock(return_value=orderbook),
        fetch_order_ids_today=MagicMock(return_value=set(known_ids)),
    )
    return tracker, h


def _sized(h, qty=10):
    h.risk.calculate_quantity.return_value = qty
    return patch("signal_engine.main.adjust_qty_for_margin", new=AsyncMock(return_value=qty))


def _net(qty):
    return patch("signal_engine.tracker.fetch_net_position", new=AsyncMock(return_value=qty))


class TestEntryTimeoutRecoveryIgnoresStaleOrders:
    @pytest.mark.asyncio
    async def test_earlier_closed_fill_recorded_in_trades_db_is_not_adopted(self):
        """The 10:50 entry (26100185347601) is closed and unregistered, so the tracker no
        longer holds it; trades.db still does. It must not be recovered as the 11:20 fill."""
        old = _entry_order("26100185347601")
        tracker, h = _timeout_harness([old], known_ids={"26100185347601"})
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal(strategy="BREAKOUT-PY"))
            h.notifier.notify_order_rejected.assert_awaited_once()
        assert tracker.tracked_count == 0

    @pytest.mark.asyncio
    async def test_new_fill_is_adopted_even_when_a_stale_twin_is_listed_first(self):
        old = _entry_order("26100185347601")
        new = _entry_order("26100112637622")
        tracker, h = _timeout_harness([old, new], known_ids={"26100185347601"})
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal(strategy="BREAKOUT-PY"))
        pos = tracker.find_position("RELIANCE", "BREAKOUT-PY")
        assert pos is not None
        assert pos.entry_order_id == "26100112637622"

    @pytest.mark.asyncio
    async def test_order_tagged_for_another_strategy_is_not_adopted(self):
        """Same symbol/side/qty under the Pine tag belongs to Pine, not to BREAKOUT-PY."""
        pine = _entry_order("PINE1", strategy="BREAKOUT")
        tracker, h = _timeout_harness([pine])
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal(strategy="BREAKOUT-PY"))
            h.notifier.notify_order_rejected.assert_awaited_once()
        assert tracker.tracked_count == 0

    @pytest.mark.asyncio
    async def test_untagged_orderbook_row_is_still_matched(self):
        """Brokers that carry no strategy field keep the previous behaviour."""
        row = _entry_order("NEW1", strategy="")
        tracker, h = _timeout_harness([row])
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal(strategy="BREAKOUT-PY"))
        assert tracker.find_position("RELIANCE", "BREAKOUT-PY") is not None


class TestRestartFallbackDoesNotTakeAnotherStrategysPosition:
    @pytest.mark.asyncio
    async def test_tcs_pine_exit_does_not_sell_breakout_py_shares(self):
        """Broker net TCS = +107, all of it BREAKOUT-PY's. Pine's TP1 (not in tracker) finds no
        position of its own, so nothing is sold."""
        tracker = _real_tracker()
        tracker.register(
            _position(symbol="TCS", strategy="BREAKOUT-PY", quantity=107, direction=Direction.LONG)
        )
        h = harness(tracker=tracker)
        with h.stack, _net(107):
            await _handle_exit(_exit_signal(strategy="BREAKOUT", symbol="TCS", tp_level="TP1"))
            h.notifier.notify_exit_no_position.assert_awaited_once_with("TCS", "BREAKOUT")
            h.send_order.assert_not_awaited()
        assert tracker.find_position("TCS", "BREAKOUT-PY").quantity == 107

    @pytest.mark.asyncio
    async def test_own_share_of_a_shared_net_position_is_recovered(self):
        """Net 140 with BREAKOUT-PY holding 107: the other 33 shares are the caller's."""
        tracker = _real_tracker()
        tracker.register(
            _position(symbol="TCS", strategy="BREAKOUT-PY", quantity=107, direction=Direction.LONG)
        )
        h = harness(tracker=tracker)
        with h.stack, _net(140):
            await _handle_exit(_exit_signal(strategy="BREAKOUT", symbol="TCS"))
        assert h.send_order.await_count == 1

    @pytest.mark.asyncio
    async def test_unshared_position_recovery_is_unchanged(self):
        tracker = _real_tracker()
        h = harness(tracker=tracker)
        with h.stack, _net(0):
            await _handle_exit(_exit_signal(strategy="BREAKOUT", symbol="TCS"))
            h.notifier.notify_exit_no_position.assert_awaited_once()


class TestOwnNetPosition:
    def test_other_strategies_signed_qty(self):
        tracker = _real_tracker()
        tracker.register(_position(symbol="TCS", strategy="A", quantity=100))
        tracker.register(
            _position(symbol="TCS", strategy="B", quantity=40, direction=Direction.SHORT)
        )
        tracker.register(_position(symbol="INFY", strategy="C", quantity=9))
        assert tracker.other_strategies_signed_qty("TCS", "NSE", "MIS", "A") == -40
        assert tracker.other_strategies_signed_qty("TCS", "NSE", "MIS", "Z") == 60
        assert tracker.other_strategies_signed_qty("TCS", "NSE", "MIS", "B") == 100
        assert tracker.other_strategies_signed_qty("INFY", "NSE", "MIS", "C") == 0

    @pytest.mark.asyncio
    async def test_own_position_subtracts_other_strategies_and_keeps_sign(self):
        tracker = _real_tracker()
        tracker.register(
            _position(symbol="TCS", strategy="B", quantity=40, direction=Direction.SHORT)
        )
        with patch(
            "signal_engine.tracker.fetch_net_position", new=AsyncMock(return_value=-171 - 40)
        ):
            own = await tracker.fetch_own_position("TCS", "A", "NSE", "MIS")
        assert own == -171

    @pytest.mark.asyncio
    async def test_unknown_broker_position_is_none_not_a_quantity(self):
        tracker = _real_tracker()
        with patch("signal_engine.tracker.fetch_net_position", new=AsyncMock(return_value=None)):
            assert await tracker.fetch_own_position("TCS", "A", "NSE", "MIS") is None


class TestExitDriverSafetyCheckSeesOnlyOwnShares:
    @pytest.mark.asyncio
    async def test_short_position_still_open_is_reported_not_treated_as_unknown(self):
        """exit_driver treats a negative quantity as 'unknown'. A short is a real negative."""
        from signal_engine.pipeline.service import make_broker_qty

        tracker = _real_tracker()
        pos = _position(symbol="ADANIPORTS", strategy="BREAKOUT-PY", quantity=171)
        with patch("signal_engine.tracker.fetch_net_position", new=AsyncMock(return_value=-171)):
            assert await make_broker_qty(tracker)(pos) == 171

    @pytest.mark.asyncio
    async def test_pine_holding_same_symbol_does_not_look_like_our_stop_failed(self):
        """Our broker stop filled (we are flat) while Pine still holds 90 long: net is 90, our
        own is 0, so the driver reconciles instead of market-selling a position we lack."""
        from signal_engine.pipeline.service import make_broker_qty

        tracker = _real_tracker()
        tracker.register(_position(symbol="TCS", strategy="BREAKOUT", quantity=90))
        pos = _position(symbol="TCS", strategy="BREAKOUT-PY", quantity=107)
        with patch("signal_engine.tracker.fetch_net_position", new=AsyncMock(return_value=90)):
            assert await make_broker_qty(tracker)(pos) == 0

    @pytest.mark.asyncio
    async def test_api_failure_stays_unknown(self):
        from signal_engine.pipeline.service import make_broker_qty

        tracker = _real_tracker()
        pos = _position(symbol="TCS", strategy="BREAKOUT-PY", quantity=107)
        with patch("signal_engine.tracker.fetch_net_position", new=AsyncMock(return_value=None)):
            assert await make_broker_qty(tracker)(pos) == -1


def _sl_settings(**kw):
    base = {
        "bracket_sl_order_type": "SL-M",
        "bracket_max_sl_retries": 5,
        "bracket_retry_delay": 0.0,
        "sl_timeout_verify_attempts": 3,
        "sl_timeout_settle_seconds": 0.0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _resting_sl(order_id="SL-LATE"):
    return {
        "symbol": "ADANIPORTS",
        "action": "BUY",
        "quantity": 171,
        "trigger_price": 1768.2,
        "order_status": "trigger pending",
        "orderid": order_id,
    }


class TestSlTimeoutDoesNotStackDuplicates:
    @pytest.mark.asyncio
    async def test_order_that_lands_after_the_first_check_is_adopted_not_resent(self):
        """The first orderbook read (right after the timeout) is empty because the server is
        still working through its backlog; the order shows up on a later read. Sending a
        second SL in between is what produced five fills on 2026-10-01."""
        send = AsyncMock(return_value=TIMEOUT)
        book = AsyncMock(side_effect=[[], [], [_resting_sl()]])
        with (
            patch.object(executor, "settings", _sl_settings()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch.object(executor, "round_to_tick", lambda p, d: p),
        ):
            result = await executor.place_sl_order(
                "ADANIPORTS", "NSE", Direction.SHORT, 171, 1768.2, "MIS", "BREAKOUT-PY"
            )
        assert result.status == OrderStatus.SUCCESS
        assert result.order_id == "SL-LATE"
        assert send.await_count == 1

    @pytest.mark.asyncio
    async def test_settle_delay_is_applied_before_each_check(self):
        send = AsyncMock(return_value=TIMEOUT)
        book = AsyncMock(return_value=[_resting_sl()])
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        with (
            patch.object(executor, "settings", _sl_settings(sl_timeout_settle_seconds=2.5)),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch.object(executor, "round_to_tick", lambda p, d: p),
            patch.object(asyncio, "sleep", fake_sleep),
        ):
            await executor.place_sl_order(
                "ADANIPORTS", "NSE", Direction.SHORT, 171, 1768.2, "MIS", "BREAKOUT-PY"
            )
        assert sleeps and sleeps[0] == 2.5

    @pytest.mark.asyncio
    async def test_genuinely_missing_order_is_still_retried(self):
        send = AsyncMock(side_effect=[TIMEOUT, TradeResult(order_id="SL-OK", status=OrderStatus.SUCCESS, message="ok")])
        book = AsyncMock(return_value=[])
        with (
            patch.object(executor, "settings", _sl_settings()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch.object(executor, "round_to_tick", lambda p, d: p),
        ):
            result = await executor.place_sl_order(
                "ADANIPORTS", "NSE", Direction.SHORT, 171, 1768.2, "MIS", "BREAKOUT-PY"
            )
        assert result.order_id == "SL-OK"
        assert send.await_count == 2


class TestRunnerSlNeedsAKnownEntry:
    @pytest.mark.asyncio
    async def test_unknown_entry_places_no_runner_stop(self):
        from signal_engine.main import _replace_runner_sl

        pos = _position(symbol="TCS", strategy="BREAKOUT", entry_price=0.0, tp=2091.75, sl=0.0)
        h = harness(tracker=_real_tracker())
        with h.stack:
            await _replace_runner_sl(pos, 75, "TP1")
            h.place_sl_order.assert_not_awaited()


class TestExitPnlComesFromTheOrdersOwnFill:
    """PY RELIANCE 2026-10-01: booked +1,871.0 from the portfolio realised delta (which also held
    Pine's 12:25 exit) and a TP price of 1171.50, against +1,362.1 from the real fill 1171.4."""

    @pytest.mark.asyncio
    async def test_other_closes_in_the_delta_do_not_leak_into_this_trade(self):
        tracker = _real_tracker()
        tracker.register(
            _position(
                symbol="RELIANCE",
                strategy="BREAKOUT-PY",
                direction=Direction.SHORT,
                quantity=257,
                entry_price=1176.8,
                fill_price=1176.7,
            )
        )
        h = harness(
            tracker=tracker,
            fetch_realised_pnl=AsyncMock(return_value=1870.96),
            fetch_order_fill_price=AsyncMock(return_value=1171.4),
        )
        with h.stack:
            await _handle_exit(
                _exit_signal(strategy="BREAKOUT-PY", symbol="RELIANCE", tp_level="TP1", tp=1171.5044)
            )
        rec = tracker._completed_trades[0]
        assert round(rec.total_pnl, 1) == 1362.1
        assert rec.exit_price == 1171.4

    @pytest.mark.asyncio
    async def test_fill_unavailable_falls_back_to_the_delta(self):
        tracker = _real_tracker()
        tracker.register(_position(quantity=50))
        h = harness(
            tracker=tracker,
            fetch_realised_pnl=AsyncMock(return_value=250.0),
            fetch_order_fill_price=AsyncMock(return_value=None),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1", tp=2510.0))
        assert tracker._completed_trades[0].total_pnl == 250.0


class TestTrackerDetectedStopCloseUsesTheStopsFill:
    @pytest.mark.asyncio
    async def test_stop_fill_beats_a_shared_symbol_ltp(self):
        """Two strategies on one symbol: the position book's ltp is the other strategy's
        execution price. The stop order's own fill is the right number."""
        risk = MagicMock()
        tracker = _real_tracker()
        tracker._risk_engine = risk
        tracker.register(
            _position(
                symbol="MCX",
                strategy="BREAKOUT-PY",
                direction=Direction.SHORT,
                quantity=92,
                entry_price=3283.7,
                fill_price=3283.7,
                sl_order_id="SLX",
                ever_seen_nonzero_qty=True,
            )
        )
        book = [{"symbol": "MCX", "quantity": 0, "ltp": 3250.0, "today_realized_pnl": 99999.0}]
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)),
            patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=0.0)),
            patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=3297.0)),
            patch("signal_engine.tracker.cancel_order", AsyncMock(return_value=False)),
            patch("signal_engine.notifier.notify_position_closed", AsyncMock()),
            patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()),
            patch.object(tracker, "_position_too_young", return_value=False),
            patch.object(tracker, "_maybe_send_day_summary", AsyncMock()),
        ):
            await tracker.check_positions()
        assert round(risk.record_close.call_args.args[0], 1) == -1223.6


class TestTimeExitIsPricedPerPosition:
    @pytest.mark.asyncio
    async def test_each_position_gets_its_own_closing_price_not_the_average(self):
        tracker = _real_tracker()
        a = _position(symbol="AAA", strategy="S1", quantity=10, entry_price=100.0, fill_price=100.0)
        b = _position(symbol="BBB", strategy="S1", quantity=10, entry_price=200.0, fill_price=200.0)
        book = [
            {"symbol": "AAA", "quantity": 0, "ltp": 110.0},
            {"symbol": "BBB", "quantity": 0, "ltp": 195.0},
        ]
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)):
            out = await tracker._time_exit_leg_pnls({"AAA:S1": a, "BBB:S1": b})
        assert out == {"AAA:S1": 100.0, "BBB:S1": -50.0}

    @pytest.mark.asyncio
    async def test_unreadable_book_prices_nothing(self):
        tracker = _real_tracker()
        a = _position(symbol="AAA", strategy="S1")
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)):
            assert await tracker._time_exit_leg_pnls({"AAA:S1": a}) == {}
