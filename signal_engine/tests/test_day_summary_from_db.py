"""The EOD summary must come from trades.db, not from counters that a restart wipes.

send_day_summary() read _day_trades / _day_pnl / _completed_trades - all in-memory, all reset
to zero by PositionTracker.__init__. On 2026-09-11 the engine restarted at 15:05 and again at
15:49, so any summary sent afterwards would have reported only the trades since that restart
and called it the day.

That is the whole of "single source of truth": trades.db is the record every performance
report and the ledger already read, and since tracker-detected closes started writing EXIT
rows it is finally COMPLETE. So the summary reads it. The in-memory counters stay for the
running day-context line on each close notification, where "since the engine started" is the
honest meaning anyway.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import db
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    db.set_trade_mode("analyze")
    yield
    db.reset_connection()


def _entry(symbol, strategy, entry, sl, tp, qty):
    db.save(
        Signal(strategy=strategy, direction=Direction.LONG, symbol=symbol,
               entry=entry, sl=sl, tp=tp, raw_message="x"),
        Order(symbol=symbol, exchange="NSE", action=Action.BUY, quantity=qty, price=0.0,
              order_type="MARKET", product="MIS", strategy_tag=strategy),
        TradeResult(status=OrderStatus.SUCCESS, order_id=f"E-{symbol}"),
    )


def _exit(symbol, strategy, entry, sl, qty, exit_price, pnl, types):
    db.save_tracker_exit(strategy=strategy, symbol=symbol, entry=entry, sl=sl, tp=0.0,
                         quantity=qty, exit_price=exit_price, pnl=pnl, exit_types=types)


class TestFetchDayTrades:
    def test_a_closed_trade_is_returned_with_its_economics(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        _exit("SBIN", "ORB", 800.0, 796.0, 10, 805.0, 50.0, ["TP1"])
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1
        row = rows[0]
        assert (row["symbol"], row["strategy"]) == ("SBIN", "ORB")
        assert row["total_pnl"] == pytest.approx(50.0)
        assert row["exit_price"] == pytest.approx(805.0)
        assert row["exit_types"] == ["TP1"]

    def test_an_open_position_is_not_reported_as_a_trade(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        assert db.fetch_day_trades("analyze") == []

    def test_declined_and_rejected_rows_are_excluded(self):
        db.save_declined(
            Signal(strategy="ORB", direction=Direction.LONG, symbol="TCS",
                   entry=4000.0, sl=3980.0, tp=4050.0, raw_message="x"),
            stage="risk_gates", reason="nope")
        assert db.fetch_day_trades("analyze") == []

    def test_another_mode_is_never_mixed_in(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        _exit("SBIN", "ORB", 800.0, 796.0, 10, 805.0, 50.0, ["TP1"])
        assert db.fetch_day_trades("live") == []

    def test_trades_from_several_strategies_all_come_back(self):
        for sym, strat in (("SBIN", "ORB"), ("TCS", "BREAKOUT")):
            _entry(sym, strat, 800.0, 796.0, 810.0, 10)
            _exit(sym, strat, 800.0, 796.0, 10, 805.0, 50.0, ["TP1"])
        assert {r["strategy"] for r in db.fetch_day_trades("analyze")} == {"ORB", "BREAKOUT"}

    def test_the_entry_row_supplies_the_stop_so_r_can_be_computed(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        _exit("SBIN", "ORB", 800.0, 796.0, 10, 805.0, 50.0, ["TP1"])
        assert db.fetch_day_trades("analyze")[0]["sl"] == pytest.approx(796.0)


class TestSummaryReadsTheDatabase:
    def _tracker(self):
        from signal_engine.tracker import PositionTracker

        risk = MagicMock()
        risk.total_last_known_capital.return_value = 100_000.0
        risk.last_known_capital_for.return_value = 100_000.0
        return PositionTracker(risk)

    @pytest.mark.asyncio
    async def test_trades_from_before_a_restart_are_still_counted(self):
        """The in-memory counters are empty here - exactly the state after a restart."""
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        _exit("SBIN", "ORB", 800.0, 796.0, 10, 805.0, 50.0, ["TP1"])
        tracker = self._tracker()
        assert tracker._day_trades == 0  # memory knows nothing

        with patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()

        assert notify.await_args.kwargs["trades"] == 1
        assert notify.await_args.kwargs["net_pnl"] == pytest.approx(50.0)
        assert [r.symbol for r in notify.await_args.kwargs["trade_records"]] == ["SBIN"]

    @pytest.mark.asyncio
    async def test_wins_and_losses_are_counted_from_the_rows(self):
        for sym, pnl in (("SBIN", 50.0), ("TCS", -30.0), ("INFY", 20.0)):
            _entry(sym, "ORB", 800.0, 796.0, 810.0, 10)
            _exit(sym, "ORB", 800.0, 796.0, 10, 805.0, pnl, ["TP1"])
        tracker = self._tracker()
        with patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()
        kwargs = notify.await_args.kwargs
        assert (kwargs["trades"], kwargs["wins"], kwargs["losses"]) == (3, 2, 1)
        assert kwargs["net_pnl"] == pytest.approx(40.0)

    @pytest.mark.asyncio
    async def test_a_time_exit_counts_as_a_trade_but_not_as_a_win_or_loss(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        _exit("SBIN", "ORB", 800.0, 796.0, 10, 805.0, 50.0, ["TIME"])
        tracker = self._tracker()
        with patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()
        kwargs = notify.await_args.kwargs
        assert (kwargs["trades"], kwargs["wins"], kwargs["losses"]) == (1, 0, 0)
        assert kwargs["time_exits"] == 1

    @pytest.mark.asyncio
    async def test_a_day_with_nothing_in_the_database_still_reports(self):
        """Every enabled channel files a report - see test_day_summary_every_channel."""
        tracker = self._tracker()
        with patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()
        assert notify.await_args.kwargs["trades"] == 0

    @pytest.mark.asyncio
    async def test_an_unreadable_database_falls_back_to_memory(self):
        """A reporting failure must not lose the day - memory is stale, but it is not nothing."""
        tracker = self._tracker()
        tracker._day_trades, tracker._day_pnl, tracker._day_wins = 2, 75.0, 2
        with patch("signal_engine.db.fetch_day_trades", side_effect=RuntimeError("locked")), \
             patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()
        assert notify.await_args.kwargs["trades"] == 2


class TestBothExitWritersProduceOneShape:
    """save_tracker_exit and save_reconciled_exit record the same fact - a closed trade - so
    the summary must read both. save_reconciled_exit wrote only {"reconciled",
    "realized_pnl"}, so a reconciled close showed +0.00 in every report."""

    def test_a_reconciled_exit_carries_its_pnl(self):
        _entry("ADANIENSOL", "BREAKINGTRADE", 1377.7, 1364.43, 1394.6, 68)
        db.save_reconciled_exit("BREAKINGTRADE", "ADANIENSOL", 1377.7, 1364.43, 1394.6,
                                68, 1370.0, -197.2, "closed while the engine was down")
        row = db.fetch_day_trades("analyze")[0]
        assert row["total_pnl"] == pytest.approx(-197.2)

    def test_a_reconciled_exit_is_labelled_as_such(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        db.save_reconciled_exit("ORB", "SBIN", 800.0, 796.0, 810.0, 10, 805.0, 50.0, "note")
        assert db.fetch_day_trades("analyze")[0]["exit_types"] == ["RECONCILED"]

    def test_tracker_and_reconciled_exits_sum_together(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        _exit("SBIN", "ORB", 800.0, 796.0, 10, 805.0, 50.0, ["TP1"])
        _entry("TCS", "ORB", 4000.0, 3980.0, 4050.0, 5)
        db.save_reconciled_exit("ORB", "TCS", 4000.0, 3980.0, 4050.0, 5, 3990.0, -50.0, "n")
        assert sum(r["total_pnl"] for r in db.fetch_day_trades("analyze")) == pytest.approx(0.0)
