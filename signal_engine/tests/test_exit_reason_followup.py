"""BUG-034 follow-up: label evidence is read before the tracker's own cancel, a late own time exit
stays TIME, SQUAREOFF is an undecided exit, and the label is computed only when it is used.

Only labels and win-rate classification change. P&L, quantity, exit price, order id and order
sending are the same as before.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import db, notifier
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker

_IST = timezone(timedelta(hours=5, minutes=30))


class _Settings:
    max_trades_per_day = 5


async def _close(pos, statuses, *, pnl_delta, cancel_flips=True, square_off_order="",
                 orderbook=None, started=None):
    """Run the poll's close booking against a stateful fake broker.

    cancel_order flips the stop to 'cancelled' (cancel_flips), as the real broker does.
    Returns (saved row kwargs, fetch_order_status calls, orderbook calls).
    """
    engine = _make_engine()
    tracker = PositionTracker(engine)
    tracker.register(pos)
    key = tracker._key(pos.symbol, pos.strategy)
    if square_off_order:
        tracker._square_off_orders[key] = square_off_order
    if started is not None:
        tracker._time_exit_started = started
    state = dict(statuses)
    status_calls = []

    async def status(order_id, _strategy):
        status_calls.append(order_id)
        return state.get(order_id, "")

    async def cancel(order_id, _strategy):
        if cancel_flips and state.get(order_id, "") not in ("complete", "cancelled", "rejected"):
            state[order_id] = "cancelled"
            return True
        return False

    book = AsyncMock(return_value=orderbook if orderbook is not None else [])
    with (
        patch("signal_engine.tracker.fetch_order_status", new=AsyncMock(side_effect=status)),
        patch("signal_engine.tracker.cancel_order", new=AsyncMock(side_effect=cancel)),
        patch("signal_engine.tracker.fetch_orderbook", new=book),
        patch.object(tracker, "_cancel_untracked_stops", new=AsyncMock(return_value=None)),
        patch("signal_engine.tracker.notifier.notify_position_closed", new=AsyncMock()),
        patch("signal_engine.tracker.db.save_tracker_exit") as save,
    ):
        await tracker._record_closed_trade(key, pos, timedelta(minutes=20), pnl_delta, _Settings())
    assert save.call_count == 1
    return save.call_args.kwargs, status_calls, book.await_count


def _pos(**kw):
    return _make_position(**kw)


class TestStopStatusReadBeforeOwnCancel:
    @pytest.mark.asyncio
    async def test_working_stop_cancelled_by_the_tracker_is_not_squareoff(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1")
        row, _, _ = await _close(pos, {"STOP-1": "open"}, pnl_delta=-100.0)
        assert row["exit_types"] == ["SL"]

    @pytest.mark.asyncio
    async def test_working_stop_with_trigger_pending_is_not_squareoff(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1")
        row, _, _ = await _close(pos, {"STOP-1": "trigger pending"}, pnl_delta=-100.0)
        assert "SQUAREOFF" not in row["exit_types"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("before", ["cancelled", "canceled", "rejected"])
    async def test_stop_already_dead_before_the_poll_is_squareoff(self, before):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1")
        row, _, _ = await _close(pos, {"STOP-1": before}, pnl_delta=50.0)
        assert row["exit_types"] == ["SQUAREOFF"]

    @pytest.mark.asyncio
    async def test_stop_filled_is_still_sl(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1")
        row, _, _ = await _close(pos, {"STOP-1": "complete"}, pnl_delta=-100.0)
        assert row["exit_types"] == ["SL"]


class TestLateOwnTimeExit:
    @staticmethod
    def _started():
        return datetime.now(_IST).replace(microsecond=0) - timedelta(minutes=5)

    @staticmethod
    def _order(tag, order_id="LATE-1", symbol="SBIN", qty=40, action="SELL"):
        stamp = (datetime.now(_IST) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "symbol": symbol, "action": action, "quantity": qty, "order_status": "complete",
            "timestamp": stamp, "orderid": order_id, "strategy": tag,
        }

    async def _unconfirmed(self, orderbook):
        pos = _pos(
            symbol="SBIN", fill_price=800.0, quantity=40, sl_order_id="STOP-1",
            time_exit_left_open=True, strategy="STRAT-A",
        )
        row, _, _ = await _close(
            pos, {"STOP-1": "cancelled"}, pnl_delta=60.0, orderbook=orderbook,
            started=self._started(),
        )
        return row

    @pytest.mark.asyncio
    async def test_unconfirmed_time_exit_found_by_exact_tag_is_time(self):
        row = await self._unconfirmed([self._order("STRAT-A")])
        assert row["exit_types"] == ["TIME"]

    @pytest.mark.asyncio
    async def test_other_strategys_tag_is_not_time(self):
        row = await self._unconfirmed([self._order("STRAT-B")])
        assert row["exit_types"] == ["SQUAREOFF"]

    @pytest.mark.asyncio
    async def test_case_different_tag_is_not_time(self):
        row = await self._unconfirmed([self._order("strat-a")])
        assert row["exit_types"] == ["SQUAREOFF"]

    @pytest.mark.asyncio
    async def test_untagged_failsafe_order_is_not_time(self):
        row = await self._unconfirmed([self._order("")])
        assert row["exit_types"] == ["SQUAREOFF"]

    @pytest.mark.asyncio
    async def test_tag_with_surrounding_space_is_time(self):
        row = await self._unconfirmed([self._order("  STRAT-A ")])
        assert row["exit_types"] == ["TIME"]

    @pytest.mark.asyncio
    async def test_order_of_another_leg_is_not_adopted(self):
        pos = _pos(
            symbol="SBIN", fill_price=800.0, quantity=40, sl_order_id="STOP-1",
            time_exit_left_open=True, strategy="STRAT-A",
        )
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.register(pos)
        tracker._square_off_orders["OTHER-LEG-KEY"] = "LATE-1"
        tracker._time_exit_started = self._started()
        with (
            patch("signal_engine.tracker.fetch_order_status",
                  new=AsyncMock(return_value="cancelled")),
            patch("signal_engine.tracker.fetch_orderbook",
                  new=AsyncMock(return_value=[self._order("STRAT-A")])),
        ):
            label = await tracker._closing_label(
                tracker._key(pos.symbol, pos.strategy), pos, False, "cancelled"
            )
        assert label == "SQUAREOFF"

    @pytest.mark.asyncio
    async def test_own_recorded_time_exit_complete_is_time(self):
        pos = _pos(
            symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", time_exit_left_open=True,
        )
        row, _, _ = await _close(
            pos, {"STOP-1": "cancelled", "TE-1": "complete"}, pnl_delta=50.0,
            square_off_order="TE-1",
        )
        assert row["exit_types"] == ["TIME"]


class TestLazyLabel:
    @pytest.mark.asyncio
    async def test_claimed_exit_makes_no_label_status_calls(self):
        pos = _pos(
            symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", claimed_exit="TRAILING_STOP",
            time_exit_left_open=True,
        )
        row, calls, book_calls = await _close(
            pos, {"STOP-1": "open", "TE-1": "complete"}, pnl_delta=10.0, square_off_order="TE-1",
        )
        assert row["exit_types"] == ["TRAILING_STOP"]
        assert calls == ["STOP-1"]  # the one read that decides whether to cancel the stop
        assert book_calls == 0

    @pytest.mark.asyncio
    async def test_non_tp_last_label_makes_no_label_status_calls(self):
        pos = _pos(
            symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", exit_types=["NO-PROGRESS"],
            time_exit_left_open=True,
        )
        row, calls, book_calls = await _close(
            pos, {"STOP-1": "open", "TE-1": "complete"}, pnl_delta=10.0, square_off_order="TE-1",
        )
        assert row["exit_types"] == ["NO-PROGRESS"]
        assert calls == ["STOP-1"]
        assert book_calls == 0

    @pytest.mark.asyncio
    async def test_unlabelled_close_still_reads_the_evidence(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", time_exit_left_open=True)
        _, calls, _ = await _close(
            pos, {"STOP-1": "cancelled", "TE-1": "cancelled"}, pnl_delta=10.0,
            square_off_order="TE-1",
        )
        assert calls == ["STOP-1", "TE-1"]  # stop read once, never re-read after the cancel


class TestNovember23StillNotSl:
    CASES = [
        ("APLAPOLLO", "STRAT-A", Direction.SHORT, 3, 1680.0, 1673.9, "26112337849391"),
        ("TATASTEEL", "STRAT-A", Direction.LONG, 127, 163.00, 163.06, "26112337856371"),
        ("TATASTEEL", "STRAT-B", Direction.LONG, 127, 163.00, 163.06, "26112337857089"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sym,strat,direction,qty,base,fill,stop_id", CASES)
    async def test_replay(self, sym, strat, direction, qty, base, fill, stop_id):
        sign = 1 if direction == Direction.LONG else -1
        pnl = round(sign * (fill - base) * qty, 2)
        pos = _pos(
            symbol=sym, strategy=strat, direction=direction, quantity=qty, fill_price=base,
            sl_order_id=stop_id, time_exit_left_open=True,
        )
        row, _, _ = await _close(
            pos, {stop_id: "cancelled", "TE-1446": "cancelled"}, pnl_delta=pnl,
            square_off_order="TE-1446",
        )
        assert row["exit_types"] == ["SQUAREOFF"]
        assert row["quantity"] == qty
        assert row["pnl"] == pytest.approx(pnl)
        assert row["exit_price"] == pytest.approx(fill)
        assert row["order_id"] == stop_id


class TestMoneyUnchanged:
    @pytest.mark.asyncio
    async def test_same_money_whatever_the_label(self):
        out = []
        for status in ("open", "cancelled", "complete"):
            pos = _pos(symbol="SBIN", fill_price=800.0, quantity=40, sl_order_id="STOP-1",
                       realized_pnl=25.0)
            row, _, _ = await _close(pos, {"STOP-1": status}, pnl_delta=-120.0)
            out.append((row["pnl"], row["quantity"], row["exit_price"], row["order_id"]))
        assert out[0] == out[1] == out[2] == (-95.0, 40, 797.0, "STOP-1")


# --- SQUAREOFF is an undecided exit ------------------------------------------------------------


@pytest.fixture
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    db.set_trade_mode("analyze")
    yield
    db.reset_connection()


def _entry(symbol, strategy, qty):
    db.save(
        Signal(strategy=strategy, direction=Direction.LONG, symbol=symbol,
               entry=100.0, sl=99.0, tp=103.0, raw_message="x"),
        Order(symbol=symbol, exchange="NSE", action=Action.BUY, quantity=qty, price=0.0,
              order_type="MARKET", product="MIS", strategy_tag=strategy),
        TradeResult(status=OrderStatus.SUCCESS, order_id=f"E-{symbol}"),
    )


def _exit(symbol, strategy, qty, pnl, types):
    db.save_tracker_exit(strategy=strategy, symbol=symbol, entry=100.0, sl=99.0, tp=0.0,
                         quantity=qty, exit_price=100.0 + pnl / qty, pnl=pnl, exit_types=types)


class TestSquareoffIsUndecided:
    def test_set_and_class(self):
        assert "SQUAREOFF" in db.NOT_DECIDED_EXIT_TYPES
        assert db._exit_class({}, ["SQUAREOFF"]) == "TIME_EXIT"
        assert db._exit_class({}, ["TP1", "SQUAREOFF"]) == "TIME_EXIT"
        assert db._exit_class({}, ["SL"]) == "STRATEGY_EXIT"

    def test_win_rate_counts_decided_only_and_money_counts_everything(self, _fresh_db):
        rows = [
            ("WIN", "STRAT-A", 10, 100.0, ["TP1"]),
            ("LOSS", "STRAT-A", 10, -40.0, ["SL"]),
            ("SQOFF", "STRAT-A", 10, -25.0, ["SQUAREOFF"]),
            ("TIMEX", "STRAT-B", 10, 15.0, ["TIME"]),
        ]
        for sym, strat, qty, pnl, types in rows:
            _entry(sym, strat, qty)
            _exit(sym, strat, qty, pnl, types)
        trades = db.fetch_day_trades("analyze")
        assert len(trades) == 4
        total = sum(r[3] for r in rows)  # 50.0

        by_class = db.pnl_by_exit_class(trades)
        assert sum(by_class.values()) == pytest.approx(total)
        assert by_class["STRATEGY_EXIT"] == pytest.approx(60.0)  # 100 - 40
        assert by_class["TIME_EXIT"] == pytest.approx(-10.0)  # -25 + 15

        tracker = PositionTracker(_make_engine())
        records, counts = tracker._day_from_db()
        assert counts["trades"] == 4
        assert (counts["wins"], counts["losses"]) == (1, 1)
        assert counts["net_pnl"] == pytest.approx(total)
        assert sum(r.total_pnl for r in records) == pytest.approx(total)

        per_strategy = notifier._group_by_strategy(records)
        stats_a = notifier._aggregate_strategy_stats(per_strategy["STRAT-A"])
        assert (stats_a["trades"], stats_a["wins"], stats_a["losses"]) == (3, 1, 1)
        assert sum(r.total_pnl for r in per_strategy["STRAT-A"]) == pytest.approx(35.0)
        assert sum(r.total_pnl for r in per_strategy["STRAT-B"]) == pytest.approx(15.0)
