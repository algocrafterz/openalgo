"""The 14:55 time exit is a real execution and must reach the ledger.

time_exit_all() closed positions through the strategy-level square-off API and booked them in
memory and on the risk engine, but wrote no trades.db row. The ledger, the fill-backed pages and
the position state then disagreed whenever the clock was what closed a position (2026-10-05: 8
positions open at EOD, BREAKOUT-PY's trades.db sum -1,146 against -3,136 in fills; 2026-09-30:
PRESTIGE and ONGC legs with no closing row).

Two questions stay separate. What happened to money: the row's pnl always counts in net P&L and
in the risk counters. What it says about the strategy: a TIME or BAD_FILL_CLOSE row is a trade
event, not a decided win or loss, so it carries exit_class and stays out of win rate.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import db
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.strategies import ORB, RSI_TP_MR
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.timeutils import IST
from signal_engine.tracker import PositionTracker


@pytest.fixture(autouse=True)
def _analyze_mode():
    db.set_trade_mode("analyze")
    yield


def _position(**overrides):
    defaults = {
        "symbol": "RELIANCE", "strategy": ORB, "product": "MIS", "quantity": 50,
        "entry_price": 2500.0, "fill_price": 2500.0, "sl": 2485.0, "tp": 2540.0,
        "direction": Direction.LONG, "entry_order_id": "E-1",
        "sig_id": "ORB-RELIANCE-20261007-0950", "ever_seen_nonzero_qty": True,
    }
    defaults.update(overrides)
    return _make_position(**defaults)


def _sell_order(symbol="RELIANCE", qty=50, order_id="CLOSE-9"):
    stamp = (datetime.now(IST) + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "symbol": symbol, "action": "SELL", "quantity": qty, "order_status": "complete",
        "timestamp": stamp, "orderid": order_id,
    }


def _closed_book(symbol="RELIANCE", ltp=2510.0):
    return [{"symbol": symbol, "quantity": 0, "ltp": ltp, "today_realized_pnl": 0.0}]


async def _run_time_exit(
    tracker, *, orderbook=None, fill=2510.0, book=None, open_qty=0, realised=0.0, open_fn=None,
):
    with (
        patch("signal_engine.tracker.cancel_all_orders", new_callable=AsyncMock),
        patch("signal_engine.tracker.close_all_positions", new_callable=AsyncMock),
        patch(
            "signal_engine.tracker.fetch_open_position",
            open_fn or AsyncMock(return_value=open_qty),
        ),
        patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=realised)),
        patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)),
        patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=orderbook)),
        patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=fill)),
        patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await tracker.time_exit_all()


def _exit_rows():
    return db._get_connection().execute(
        "SELECT id, strategy, symbol, quantity, order_id, fill_price, sig_id, message, context "
        "FROM trades WHERE direction = 'EXIT' ORDER BY id"
    ).fetchall()


def _tracker_with(position):
    engine = _make_engine()
    engine._state(position.strategy).open_positions = 1
    tracker = PositionTracker(engine)
    tracker.register(position)
    return tracker, engine


@pytest.mark.asyncio
class TestTimeExitWritesTheLedgerRow:
    async def test_one_exit_row_per_closed_position_with_full_lineage(self):
        tracker, _ = _tracker_with(_position())
        await _run_time_exit(tracker, orderbook=[_sell_order()], book=_closed_book())
        rows = _exit_rows()
        assert len(rows) == 1
        _id, strategy, symbol, qty, order_id, fill, sig_id, message, context = rows[0]
        assert (strategy, symbol, qty) == (ORB, "RELIANCE", 50)
        assert (order_id, fill, sig_id) == ("CLOSE-9", 2510.0, "ORB-RELIANCE-20261007-0950")
        ctx = json.loads(context)
        assert ctx["pnl"] == pytest.approx(500.0)
        assert ctx["exit_types"] == ["TIME"]
        assert ctx["exit_class"] == "TIME_EXIT"
        assert ctx["entry_order_id"] == "E-1"
        assert (ctx["exit_order_id"], ctx["exit_fill"]) == ("CLOSE-9", 2510.0)
        assert ctx["includes_leg_pnl"] == pytest.approx(0.0)
        assert "TIME" in message

    async def test_the_day_total_and_class_come_from_the_row(self):
        tracker, _ = _tracker_with(_position())
        await _run_time_exit(tracker, orderbook=[_sell_order()], book=_closed_book())
        trades = db.fetch_day_trades("analyze")
        assert len(trades) == 1
        assert trades[0]["total_pnl"] == pytest.approx(500.0)
        assert trades[0]["exit_class"] == "TIME_EXIT"
        assert trades[0]["exit_types"] == ["TIME"]

    async def test_a_position_with_a_partial_leg_is_counted_once(self):
        """TP1 leg +372.40 already realised; the time exit closes the 20-share runner."""
        position = _position(quantity=20, original_quantity=50)
        position.realized_pnl = 372.40
        position.exit_types = ["TP1"]
        db.save(
            Signal(strategy=ORB, direction=Direction.EXIT, symbol="RELIANCE", entry=0.0, sl=0.0,
                   tp=0.0, raw_message="x", sig_id="ORB-RELIANCE-20261007-0950",
                   context={"pnl": 372.40, "exit_types": ["TP1"], "partial": True}),
            Order(symbol="RELIANCE", exchange="NSE", action=Action.SELL, quantity=30, price=0.0,
                  order_type="MARKET", product="MIS", strategy_tag=ORB),
            TradeResult(status=OrderStatus.SUCCESS, order_id="LEG-1", message="success"),
        )
        tracker, _ = _tracker_with(position)
        await _run_time_exit(
            tracker, orderbook=[_sell_order(qty=20)], fill=2510.0, book=_closed_book()
        )
        final = [r for r in _exit_rows() if r[4] == "CLOSE-9"][0]
        ctx = json.loads(final[8])
        assert ctx["pnl"] == pytest.approx(372.40 + 200.0)  # cumulative, as book_close writes it
        assert ctx["includes_leg_pnl"] == pytest.approx(372.40)
        assert ctx["exit_types"] == ["TP1", "TIME"]
        trades = db.fetch_day_trades("analyze")
        assert len(trades) == 1 and trades[0]["total_pnl"] == pytest.approx(572.40)

    async def test_without_an_orderbook_match_the_row_still_exists_with_the_position_price(self):
        tracker, _ = _tracker_with(_position())
        await _run_time_exit(tracker, orderbook=None, fill=None, book=_closed_book(ltp=2510.0))
        rows = _exit_rows()
        assert len(rows) == 1
        assert rows[0][4] == "-TRACKER-"  # no order id knowable
        assert json.loads(rows[0][8])["pnl"] == pytest.approx(500.0)

    async def test_the_risk_engine_and_ledger_agree_on_the_loss(self):
        tracker, engine = _tracker_with(_position())
        await _run_time_exit(
            tracker, orderbook=[_sell_order()], fill=2490.0, book=_closed_book(ltp=2490.0)
        )
        assert db.fetch_day_trades("analyze")[0]["total_pnl"] == pytest.approx(-500.0)
        assert engine._state(ORB).daily_realised_loss == pytest.approx(500.0)

    async def test_a_cnc_position_gets_no_row(self):
        tracker, _ = _tracker_with(_position(strategy=RSI_TP_MR, product="CNC"))
        await _run_time_exit(tracker, orderbook=[_sell_order()], book=_closed_book())
        assert _exit_rows() == []

    async def test_a_position_the_broker_still_holds_gets_no_closing_row(self):
        """A row would claim a close that did not happen."""
        tracker, _ = _tracker_with(_position())
        await _run_time_exit(tracker, orderbook=[_sell_order()], book=_closed_book(), open_qty=50)
        assert _exit_rows() == []


@pytest.mark.asyncio
class TestExecutionEventsAreNotDecidedWinsOrLosses:
    """Money counts; strategy evidence does not. The day summary's win rate is a statement
    about the entry logic, and the clock or the guard closing a position says nothing about it."""

    async def test_a_time_exit_counts_in_net_pnl_but_not_in_wins_or_losses(self):
        tracker, _ = _tracker_with(_position())
        await _run_time_exit(
            tracker, orderbook=[_sell_order()], fill=2490.0, book=_closed_book(ltp=2490.0)
        )
        fresh = PositionTracker(_make_engine())
        _, counts = fresh._day_from_db()
        assert counts["trades"] == 1
        assert counts["net_pnl"] == pytest.approx(-500.0)
        assert (counts["wins"], counts["losses"]) == (0, 0)
        assert counts["time_exits"] == 1

    async def test_a_bad_fill_close_counts_in_net_pnl_but_not_in_wins_or_losses(self):
        db.save_execution_exit(
            strategy="BREAKOUT-PY", symbol="PFC", signal_entry=325.6, sl=324.62, tp=327.07,
            quantity=930, entry_order_id="E", exit_order_id="C", entry_fill=326.1,
            exit_fill=326.0, pnl=-93.0, exit_type="ENTRY_REJECTED_SLIPPAGE", reason="slipped",
            slippage_r=0.51, slippage_rs=465.0, sig_id="BREAKOUT-PY-PFC-20261006-1120",
        )
        _, counts = PositionTracker(_make_engine())._day_from_db()
        assert counts["trades"] == 1
        assert counts["net_pnl"] == pytest.approx(-93.0)
        assert (counts["wins"], counts["losses"]) == (0, 0)
        assert counts["execution_exits"] == 1

    async def test_every_row_carries_a_class_so_reports_can_split_strategy_pnl(self):
        db.save_tracker_exit(
            strategy=ORB, symbol="SBIN", entry=100.0, sl=99.0, tp=102.0, quantity=1,
            exit_price=102.0, pnl=2.0, exit_types=["TP1"],
        )
        assert db.fetch_day_trades("analyze")[0]["exit_class"] == "STRATEGY_EXIT"
        by_class = db.pnl_by_exit_class(db.fetch_day_trades("analyze"))
        assert by_class == {"STRATEGY_EXIT": pytest.approx(2.0)}


@pytest.mark.asyncio
class TestTimeExitAndThePollDoNotBookTheSameClose:
    """The tracker polls every few seconds and sees a position the square-off just closed. On
    2026-09-24 (IOC), 09-29 (HINDPETRO x2), 09-30 (NAUKRI) and 10-01 (TCS BREAKOUT-PY) the poll
    booked the close within 8 seconds of the 14:45 time exit and the time exit then booked it
    again. With a ledger row per time exit that would write the same loss twice and take it
    twice on the risk counters."""

    async def test_the_flag_is_held_through_verification_and_booking_then_cleared(self):
        tracker, _ = _tracker_with(_position())
        seen = {}

        async def open_position(*_a, **_k):
            seen["during_verify"] = tracker._time_exit_active
            return 0

        await _run_time_exit(
            tracker, orderbook=[_sell_order()], book=_closed_book(), open_fn=open_position
        )
        assert seen["during_verify"] is True
        assert tracker._time_exit_active is False

    async def test_the_flag_is_cleared_even_if_booking_fails(self):
        tracker, _ = _tracker_with(_position())
        with patch.object(tracker, "_book_time_exit_trades", AsyncMock(side_effect=RuntimeError)):
            with pytest.raises(RuntimeError):
                await _run_time_exit(tracker, orderbook=[_sell_order()], book=_closed_book())
        assert tracker._time_exit_active is False

    async def test_the_poll_does_nothing_while_a_time_exit_is_running(self):
        tracker, _ = _tracker_with(_position())
        tracker._time_exit_active = True
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_closed_book())),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
        ):
            await tracker.check_positions()
        assert tracker.tracked_count == 1
        assert _exit_rows() == []

    async def test_a_position_already_booked_is_not_booked_again_by_the_time_exit(self):
        from signal_engine.config import settings

        tracker, engine = _tracker_with(_position())
        snapshot = dict(tracker._positions)  # what time_exit_all took before the poll ran
        (key, pos), = snapshot.items()
        with (
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch.object(tracker, "_maybe_send_day_summary", AsyncMock()),
        ):
            await tracker.book_close(
                pos, pnl_delta=-500.0, exit_price=2490.0, exit_types=["SL"], hold_minutes=5,
                max_trades=5, new_realised_pnl=-500.0,
            )
        # what the poll's close path does after book_close(): the risk engine and the registry
        engine.record_close(-500.0, strategy=pos.strategy, symbol=pos.symbol)
        tracker.unregister(pos.symbol, pos.strategy)
        with (
            patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=-500.0)),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_closed_book())),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=[_sell_order()])),
            patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=2490.0)),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
        ):
            await tracker._book_time_exit_trades(
                snapshot, settings, datetime.now(IST).replace(microsecond=0)
            )
        assert len(_exit_rows()) == 1  # the poll's row only
        assert engine._state(ORB).daily_realised_loss == pytest.approx(500.0)  # once, not twice
        assert len(db.fetch_day_trades("analyze")) == 1

    async def test_two_strategies_never_adopt_the_same_order_id(self):
        """BREAKOUT and BREAKOUT-PY both hold PFC 930 in the sandbox; one order in the book."""
        engine = _make_engine()
        tracker = PositionTracker(engine)
        for strategy, sig in ((ORB, "A"), ("BREAKOUT-PY", "B")):
            engine._state(strategy).open_positions = 1
            tracker.register(
                _position(
                    symbol="PFC", strategy=strategy, quantity=930, entry_price=325.0,
                    fill_price=325.0, sl=324.0, tp=327.0, sig_id=sig,
                )
            )
        await _run_time_exit(
            tracker, orderbook=[_sell_order("PFC", 930, "CLOSE-7")], fill=326.0,
            book=_closed_book("PFC", 326.0),
        )
        ids = sorted(r[4] for r in _exit_rows())
        assert ids == ["-TRACKER-", "CLOSE-7"]
