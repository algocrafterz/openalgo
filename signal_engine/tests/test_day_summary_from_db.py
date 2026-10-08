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

    def test_a_partial_exit_leg_is_not_counted_as_its_own_trade(self):
        """2026-09-21: a partial leg's own EXIT row (written by the generic save() at order-
        placement time, context={}) used to default to total_pnl=0.0 and be counted as a
        completed trade here - inflating Telegram's "Trades: N" and, since 0.0 >= 0, its win
        rate too (BHARTIARTL/RELIANCE's partial legs turned 5 real closed positions into a
        reported 8 "trades" at 88% win rate). The partial leg's own economics are not lost -
        they are already folded into the final row's cumulative pnl."""
        _entry("BHARTIARTL", "BREAKOUT", 1842.10, 1849.43, 1836.85, 180)
        db.save(  # TP1 partial leg - the ONLY row a partial exit writes, raw context
            Signal(strategy="BREAKOUT", direction=Direction.EXIT, symbol="BHARTIARTL",
                   entry=0.0, sl=0.0, tp=1836.85, raw_message="x"),
            Order(symbol="BHARTIARTL", exchange="NSE", action=Action.BUY, quantity=54,
                  price=0.0, order_type="MARKET", product="MIS", strategy_tag="BREAKOUT"),
            TradeResult(status=OrderStatus.SUCCESS, order_id="TP1-LEG"),
        )
        _exit("BHARTIARTL", "BREAKOUT", 1842.10, 1849.43, 63, 1833.80, 1382.40, ["TP1", "TP2"])
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1
        assert rows[0]["total_pnl"] == pytest.approx(1382.40)


def _partial_leg(symbol, strategy, qty, pnl, *, marked=True, sig_id=None):
    """A TP partial leg as main._book_exit_result writes it: the generic save(), with the leg's
    own pnl in the context (since 2026-09-22) and, now, an explicit partial marker."""
    context = {"pnl": pnl, "exit_types": ["TP1"]}
    if marked:
        context["partial"] = True
    db.save(
        Signal(strategy=strategy, direction=Direction.EXIT, symbol=symbol, entry=0.0, sl=0.0,
               tp=0.0, raw_message="x", context=context, sig_id=sig_id),
        Order(symbol=symbol, exchange="NSE", action=Action.SELL, quantity=qty, price=0.0,
              order_type="MARKET", product="MIS", strategy_tag=strategy),
        TradeResult(status=OrderStatus.SUCCESS, order_id=f"LEG-{symbol}-{qty}", message="success"),
    )


class TestPartialLegsAreNotDoubleCounted:
    """2026-10-06: the day's engine P&L ran 1,164.3 above the broker fills. The final EXIT row's
    pnl is CUMULATIVE (book_close: pos.realized_pnl + pnl_delta, so it already contains every
    partial leg), and since 2026-09-22 each partial leg's own row carries its pnl too. Reading
    both counted the leg twice and counted the leg as a trade."""

    def test_reliance_orb_2026_10_06_counts_once(self):
        _entry("RELIANCE", "ORB", 1203.7, 1187.98, 1215.0, 57)
        _partial_leg("RELIANCE", "ORB", 28, 375.20)  # TP1 leg, +375.20
        _exit("RELIANCE", "ORB", 1203.7, 1187.98, 29, 1210.1, 560.80, ["TP1"])  # cumulative
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1
        assert sum(r["total_pnl"] for r in rows) == pytest.approx(560.80)

    def test_a_partial_row_without_the_marker_is_still_recognised(self):
        """Rows written between 2026-09-22 and this fix carry pnl but no marker; their message
        is the generic save()'s 'success', which no final-close writer uses."""
        _entry("RELIANCE", "ORB", 1203.7, 1187.98, 1215.0, 57)
        _partial_leg("RELIANCE", "ORB", 28, 375.20, marked=False)
        _exit("RELIANCE", "ORB", 1203.7, 1187.98, 29, 1210.1, 560.80, ["TP1"])
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1 and rows[0]["total_pnl"] == pytest.approx(560.80)

    def test_a_reconciled_close_is_never_mistaken_for_a_partial_leg(self):
        _entry("SBIN", "ORB", 800.0, 796.0, 810.0, 10)
        db.save_reconciled_exit(
            strategy="ORB", symbol="SBIN", entry=800.0, sl=796.0, tp=0.0, quantity=10,
            pnl=-40.0, fill_price=796.0, note="closed before restart", sig_id=None,
        )
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1 and rows[0]["total_pnl"] == pytest.approx(-40.0)

    def test_two_strategies_each_close_a_partial_position_on_the_same_symbol(self):
        """UJJIVANSFB: WATCHLIST 363.32 -> 597.72 and BREAKINGTRADE 332.76 -> 535.80."""
        for strategy, qty, leg, final in (
            ("BREAKINGTRADE-WATCHLIST", 293, 363.32, 597.72),
            ("BREAKINGTRADE", 282, 332.76, 535.80),
        ):
            _entry("UJJIVANSFB", strategy, 66.45, 67.0, 65.0, 586)
            _partial_leg("UJJIVANSFB", strategy, qty, leg)
            _exit("UJJIVANSFB", strategy, 66.45, 67.0, qty, 65.65, final, ["TP1"])
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 2
        assert sum(r["total_pnl"] for r in rows) == pytest.approx(597.72 + 535.80)


def _final(symbol, strategy, qty, pnl, types, *, includes=None, sig_id=None):
    db.save_tracker_exit(
        strategy=strategy, symbol=symbol, entry=100.0, sl=99.0, tp=0.0, quantity=qty,
        exit_price=101.0, pnl=pnl, exit_types=types, sig_id=sig_id, includes_leg_pnl=includes,
    )


class TestALegIsOnlySkippedWhenAFinalRowContainsIt:
    """Reviewed against the real 2026-09-30/10-01 rows: skipping every partial leg dropped real
    money wherever the final row did NOT contain the leg."""

    def test_a_leg_whose_runner_was_closed_by_the_time_exit_still_counts(self):
        """PRESTIGE 2026-09-30: TP1 +422.10, then time_exit_all, which writes no EXIT row."""
        _entry("PRESTIGE", "BREAKINGTRADE-WATCHLIST", 100.0, 99.0, 102.0, 100)
        _partial_leg("PRESTIGE", "BREAKINGTRADE-WATCHLIST", 50, 422.10)
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1
        assert rows[0]["total_pnl"] == pytest.approx(422.10)
        assert rows[0]["exit_types"] == ["TP1"]

    def test_a_leg_with_no_entry_row_still_counts(self):
        """TCS 2026-10-01: a leg of -243.60 with neither an entry row nor a final row."""
        _partial_leg("TCS", "BREAKOUT", 40, -243.60)
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1 and rows[0]["total_pnl"] == pytest.approx(-243.60)

    def test_the_sbin_race_final_booked_before_the_leg_row_counts_both(self):
        """SBIN 2026-09-30: the tracker poll booked the SL close (+328.50) BEFORE the TP1 leg
        (+139.50) finished, so the final row has the LOWER id and does not contain the leg."""
        _entry("SBIN", "BREAKOUT", 100.0, 99.0, 102.0, 100)
        _final("SBIN", "BREAKOUT", 50, 328.50, ["SL"], includes=0.0)
        _partial_leg("SBIN", "BREAKOUT", 50, 139.50)
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1
        assert rows[0]["total_pnl"] == pytest.approx(468.0)

    def test_the_same_race_without_the_marker_is_read_from_the_exit_labels(self):
        _entry("SBIN", "BREAKOUT", 100.0, 99.0, 102.0, 100)
        _final("SBIN", "BREAKOUT", 50, 328.50, ["SL"])  # legacy row: no includes_leg_pnl
        _partial_leg("SBIN", "BREAKOUT", 50, 139.50)  # TP1 leg, final is labelled SL only
        rows = db.fetch_day_trades("analyze")
        assert rows[0]["total_pnl"] == pytest.approx(468.0)

    def test_a_restart_between_tp1_and_the_close_counts_the_leg_once(self):
        """The recovered position restarts with realized_pnl 0, so its final row records
        includes_leg_pnl=0 and holds only the last leg."""
        _entry("RELIANCE", "BREAKOUT", 100.0, 99.0, 102.0, 100)
        _partial_leg("RELIANCE", "BREAKOUT", 50, 375.20)
        _final("RELIANCE", "BREAKOUT", 50, 185.60, ["TP1"], includes=0.0)
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1 and rows[0]["total_pnl"] == pytest.approx(560.80)

    def test_a_final_row_that_records_it_includes_the_leg_is_not_added_to(self):
        _entry("RELIANCE", "ORB", 100.0, 99.0, 102.0, 100)
        _partial_leg("RELIANCE", "ORB", 50, 375.20)
        _final("RELIANCE", "ORB", 50, 560.80, ["TP1"], includes=375.20)
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1 and rows[0]["total_pnl"] == pytest.approx(560.80)

    def test_two_legs_and_a_final_that_contains_both(self):
        _entry("ICICIBANK", "BREAKOUT", 100.0, 99.0, 102.0, 100)
        _partial_leg("ICICIBANK", "BREAKOUT", 30, 724.40)
        _partial_leg("ICICIBANK", "BREAKOUT", 30, 276.50)
        _final("ICICIBANK", "BREAKOUT", 40, 1280.90, ["TP1", "TP2"])  # legacy, both labels
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1 and rows[0]["total_pnl"] == pytest.approx(1280.90)

    def test_a_reconciled_close_is_taken_to_contain_every_leg(self):
        """The broker's realised figure for the position is the whole position."""
        _entry("SBIN", "ORB", 100.0, 99.0, 102.0, 100)
        _partial_leg("SBIN", "ORB", 50, 100.0)
        db.save_reconciled_exit(
            strategy="ORB", symbol="SBIN", entry=100.0, sl=99.0, tp=0.0, quantity=50,
            pnl=180.0, fill_price=101.0, note="closed before restart", sig_id=None,
        )
        rows = db.fetch_day_trades("analyze")
        assert len(rows) == 1 and rows[0]["total_pnl"] == pytest.approx(180.0)


class TestExitsPairWithTheirOwnEntry:
    """fetch_day_trades keyed entries by (symbol, strategy) with the latest winning, so with a
    re-entry every exit took the LAST entry's stop and size. sig_id is the lineage key; with no
    sig_id the nearest PRECEDING entry is the pairing."""

    def _entry_with(self, symbol, strategy, sl, qty, sig_id):
        db.save(
            Signal(strategy=strategy, direction=Direction.LONG, symbol=symbol, entry=100.0,
                   sl=sl, tp=102.0, raw_message="x", sig_id=sig_id),
            Order(symbol=symbol, exchange="NSE", action=Action.BUY, quantity=qty, price=0.0,
                  order_type="MARKET", product="MIS", strategy_tag=strategy),
            TradeResult(status=OrderStatus.SUCCESS, order_id=f"E-{sig_id}"),
        )

    def _exit_with(self, symbol, strategy, sig_id, pnl):
        db.save_tracker_exit(
            strategy=strategy, symbol=symbol, entry=100.0, sl=0.0, tp=0.0, quantity=1,
            exit_price=101.0, pnl=pnl, exit_types=["TP1"], sig_id=sig_id,
        )

    def test_reentry_pairs_each_exit_by_sig_id(self):
        self._entry_with("ICICIBANK", "BREAKOUT-PY", 99.0, 10, "BREAKOUT-PY-ICICIBANK-1000")
        self._entry_with("ICICIBANK", "BREAKOUT-PY", 98.0, 20, "BREAKOUT-PY-ICICIBANK-1025")
        # the FIRST trade closes after the second was entered
        self._exit_with("ICICIBANK", "BREAKOUT-PY", "BREAKOUT-PY-ICICIBANK-1000", 10.0)
        self._exit_with("ICICIBANK", "BREAKOUT-PY", "BREAKOUT-PY-ICICIBANK-1025", 20.0)
        rows = {r["total_pnl"]: r for r in db.fetch_day_trades("analyze")}
        assert (rows[10.0]["sl"], rows[10.0]["quantity"]) == (99.0, 10)
        assert (rows[20.0]["sl"], rows[20.0]["quantity"]) == (98.0, 20)

    def test_without_sig_id_the_nearest_preceding_entry_is_used(self):
        self._entry_with("SBIN", "ORB", 99.0, 10, None)
        self._exit_with("SBIN", "ORB", None, 10.0)
        self._entry_with("SBIN", "ORB", 98.0, 20, None)
        self._exit_with("SBIN", "ORB", None, 20.0)
        rows = {r["total_pnl"]: r for r in db.fetch_day_trades("analyze")}
        assert (rows[10.0]["sl"], rows[10.0]["quantity"]) == (99.0, 10)
        assert (rows[20.0]["sl"], rows[20.0]["quantity"]) == (98.0, 20)


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
