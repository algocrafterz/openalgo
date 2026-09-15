"""BREAKINGTRADE and BREAKINGTRADE-WATCHLIST collide by design on the same symbol, and the
broker nets positions by (symbol, exchange, product) alone - no strategy dimension. Two
engine legs each computing an independent P&L for what the broker treats as one blended
position produced the 2026-09-15 reconciliation mismatch (engine -1,573.43 vs broker
-1,127.04): LTM, SUPREMEIND and HDFCBANK were each traded by both strategies that day.

For reporting, BREAKINGTRADE (the confirmed signal) owns an overlapping symbol's P&L for the
day; BREAKINGTRADE-WATCHLIST's leg on that same symbol is dropped so it is not double-counted.
A symbol only one of the two strategies traded is unaffected either way.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import db
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.tracker import TradeRecord, _dedupe_breakingtrade_overlap


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
        TradeResult(status=OrderStatus.SUCCESS, order_id=f"E-{symbol}-{strategy}"),
    )


def _exit(symbol, strategy, entry, sl, qty, exit_price, pnl, types):
    db.save_tracker_exit(strategy=strategy, symbol=symbol, entry=entry, sl=sl, tp=0.0,
                         quantity=qty, exit_price=exit_price, pnl=pnl, exit_types=types)


def _record(symbol, strategy, pnl=0.0):
    return TradeRecord(symbol=symbol, direction="LONG", strategy=strategy, entry_price=1.0,
                        exit_price=1.0, original_qty=1, total_pnl=pnl, r_multiple=None,
                        exit_types=["SL"])


class TestDedupeBreakingtradeOverlapUnit:
    """Direct tests of the pure filter, independent of the database."""

    def test_watchlist_leg_dropped_when_breakingtrade_traded_the_same_symbol(self):
        records = [_record("LTM", "BREAKINGTRADE-WATCHLIST", -1359.40), _record("LTM", "BREAKINGTRADE", -1359.40)]
        result = _dedupe_breakingtrade_overlap(records)
        assert [r.strategy for r in result] == ["BREAKINGTRADE"]

    def test_watchlist_only_symbol_is_kept(self):
        records = [_record("INDHOTEL", "BREAKINGTRADE-WATCHLIST", -688.80)]
        result = _dedupe_breakingtrade_overlap(records)
        assert result == records

    def test_breakingtrade_only_symbol_is_kept(self):
        records = [_record("KAYNES", "BREAKINGTRADE", 100.0)]
        result = _dedupe_breakingtrade_overlap(records)
        assert result == records

    def test_non_overlapping_symbols_across_both_strategies_are_all_kept(self):
        records = [
            _record("INDHOTEL", "BREAKINGTRADE-WATCHLIST", -688.80),  # watchlist-only
            _record("KAYNES", "BREAKINGTRADE", 100.0),                # breakingtrade-only
        ]
        result = _dedupe_breakingtrade_overlap(records)
        assert result == records

    def test_other_strategies_are_never_touched(self):
        records = [_record("SBIN", "ORB", 50.0), _record("SBIN", "BREAKOUT", -20.0)]
        result = _dedupe_breakingtrade_overlap(records)
        assert result == records

    def test_case_insensitive_symbol_and_strategy_match(self):
        records = [_record("ltm", "breakingtrade-watchlist", -1.0), _record("LTM", "BreakingTrade", -1.0)]
        result = _dedupe_breakingtrade_overlap(records)
        assert len(result) == 1
        assert result[0].strategy.lower() == "breakingtrade"


class TestDedupeAppliesToTheDaySummary:
    """Integration through PositionTracker.send_day_summary(), matching
    test_day_summary_from_db.py's TestSummaryReadsTheDatabase style."""

    def _tracker(self):
        from signal_engine.tracker import PositionTracker

        risk = MagicMock()
        risk.total_last_known_capital.return_value = 100_000.0
        risk.last_known_capital_for.return_value = 100_000.0
        return PositionTracker(risk)

    @pytest.mark.asyncio
    async def test_overlapping_symbol_is_counted_once_under_breakingtrade(self):
        _entry("LTM", "BREAKINGTRADE-WATCHLIST", 4485.10, 4520.0, 4400.0, 10)
        _exit("LTM", "BREAKINGTRADE-WATCHLIST", 4485.10, 4520.0, 10, 4388.0, -1359.40, ["SL"])
        _entry("LTM", "BREAKINGTRADE", 4462.0, 4500.0, 4390.0, 10)
        _exit("LTM", "BREAKINGTRADE", 4462.0, 4500.0, 10, 4364.9, -1359.40, ["SL"])

        tracker = self._tracker()
        with patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()

        kwargs = notify.await_args.kwargs
        assert kwargs["trades"] == 1
        assert kwargs["net_pnl"] == pytest.approx(-1359.40)
        assert [r.strategy for r in kwargs["trade_records"]] == ["BREAKINGTRADE"]

    @pytest.mark.asyncio
    async def test_non_overlapping_symbols_still_both_count(self):
        # INDHOTEL: watchlist only. KAYNES: breakingtrade only.
        _entry("INDHOTEL", "BREAKINGTRADE-WATCHLIST", 731.45, 724.1, 744.5, 10)
        _exit("INDHOTEL", "BREAKINGTRADE-WATCHLIST", 731.45, 724.1, 10, 725.8, -688.80, ["SL"])
        _entry("KAYNES", "BREAKINGTRADE", 3410.0, 3440.0, 3350.0, 5)
        _exit("KAYNES", "BREAKINGTRADE", 3410.0, 3440.0, 5, 3399.2, 100.0, ["TP1"])

        tracker = self._tracker()
        with patch("signal_engine.notifier.notify_day_summary",
                   AsyncMock(return_value=True)) as notify, \
             patch("signal_engine.tracker._summary_already_sent_today", return_value=False), \
             patch("signal_engine.tracker._mark_summary_sent"):
            await tracker.send_day_summary()

        kwargs = notify.await_args.kwargs
        assert kwargs["trades"] == 2
        assert {r.symbol for r in kwargs["trade_records"]} == {"INDHOTEL", "KAYNES"}
