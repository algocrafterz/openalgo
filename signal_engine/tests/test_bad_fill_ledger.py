"""Bad-entry auto-closes are real executions and must reach the ledger.

2026-10-06 PFC (BREAKOUT-PY signal 325.60): the entry filled 326.10, 0.51R against a 0.50R cap, so
the guard closed it at once (SELL 930 @ 326.00, a real -93.0). The engine wrote no trades.db row
for either order, told the risk engine the loss was 0.0, and the EOD ledger reported two broker
fills "with no matching engine order". The guard also discarded send_order()'s result, so a failed
close would have left a live position with its stop already cancelled and no tracking at all.

The event is recorded as its own exit type, never as a normal strategy exit.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import db
from signal_engine.main import _establish_position
from signal_engine.models import (
    Action,
    Direction,
    Order,
    OrderStatus,
    Signal,
    TradeResult,
)

SLIPPAGE = "ENTRY_REJECTED_SLIPPAGE"  # the cause, kept in context["reason"]
TP_OVERSHOT = "ENTRY_REJECTED_TP_OVERSHOT"
BAD_FILL = "BAD_FILL_CLOSE"  # the classification reports and risk see


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    db.set_trade_mode("analyze")
    yield
    db.reset_connection()


def _signal(entry=325.60, sl=324.62, tp=327.07):
    return Signal(
        strategy="BREAKOUT-PY", direction=Direction.LONG, symbol="PFC", entry=entry, sl=sl, tp=tp,
        raw_message="x", sig_id="BREAKOUT-PY-PFC-20261006-1120",
    )


def _order(qty=930):
    return Order(
        symbol="PFC", exchange="NSE", action=Action.BUY, quantity=qty, price=0.0,
        order_type="MARKET", product="MIS", strategy_tag="BREAKOUT-PY",
    )


async def _establish(
    signal=None, *, fill=326.10, close_result=None, close_fill=326.00, broker_qty=0,
    realised_delta=-90.0, max_slip_r=0.5, cancel_ok=True, recent_fill=None, risk=None,
):
    """Run _establish_position with the broker edges mocked. Returns (established, mocks)."""
    signal = signal or _signal()
    close_result = close_result or TradeResult(status=OrderStatus.SUCCESS, order_id="CLOSE-1")
    settings = SimpleNamespace(
        exchange="NSE", product="MIS", max_entry_slippage_r=max_slip_r, bracket_enabled=True,
    )
    mocks = SimpleNamespace(
        send_order=AsyncMock(return_value=close_result),
        cancel_order=AsyncMock(return_value=cancel_ok),
        place_bracket=AsyncMock(side_effect=["SL-1", "SL-2"]),
        notifier=MagicMock(),
        risk=risk if risk is not None else MagicMock(),
        tracker=MagicMock(),
    )
    for name in ("notify_order_rejected", "notify_exit_failed", "notify_entry_filled"):
        setattr(mocks.notifier, name, AsyncMock())
    mocks.tracker.fetch_own_position = AsyncMock(return_value=broker_qty)
    entry_result = TradeResult(status=OrderStatus.SUCCESS, order_id="ENTRY-1")
    with (
        patch("signal_engine.main.settings", settings),
        patch("signal_engine.main.send_order", mocks.send_order),
        patch("signal_engine.main.cancel_order", mocks.cancel_order),
        patch("signal_engine.main._place_entry_bracket", mocks.place_bracket),
        patch("signal_engine.main._fetch_entry_fill", AsyncMock(return_value=fill)),
        patch(
            "signal_engine.main._find_recent_market_exit_fill",
            AsyncMock(return_value=recent_fill),
        ),
        patch("signal_engine.main.fetch_order_fill_price", AsyncMock(return_value=close_fill)),
        patch(
            "signal_engine.main._book_realised_pnl_delta",
            AsyncMock(return_value=(realised_delta, 0.0)),
        ),
        patch("signal_engine.main.notifier", mocks.notifier),
        patch("signal_engine.main.risk_engine", mocks.risk),
        patch("signal_engine.main.tracker", mocks.tracker),
    ):
        established = await _establish_position(signal, _order(), 930, entry_result)
    mocks.entry_result = entry_result
    return established, mocks


def _rows():
    conn = db._get_connection()
    return conn.execute(
        "SELECT id, direction, status, order_id, fill_price, sig_id, message, context "
        "FROM trades ORDER BY id"
    ).fetchall()


@pytest.mark.asyncio
class TestSlippageCloseReachesTheLedger:
    async def test_pfc_2026_10_06_records_entry_then_exit_with_the_real_loss(self):
        established, mocks = await _establish()
        assert established is False  # auto-closed: the caller must not register or re-save
        rows = _rows()
        assert [r[1] for r in rows] == ["LONG", "EXIT"]  # entry first, so it never reads as open
        entry_row, exit_row = rows
        assert (entry_row[2], entry_row[3], entry_row[4]) == ("SUCCESS", "ENTRY-1", 326.10)
        assert (exit_row[2], exit_row[3], exit_row[5]) == (
            "SUCCESS", "CLOSE-1", "BREAKOUT-PY-PFC-20261006-1120",
        )
        ctx = json.loads(exit_row[7])
        assert ctx["pnl"] == pytest.approx(-93.0)
        assert ctx["exit_types"] == [BAD_FILL]
        assert ctx["exit_class"] == BAD_FILL
        assert ctx["execution_exit"] is True
        assert (ctx["signal_entry"], ctx["entry_fill"], ctx["exit_fill"]) == (325.6, 326.1, 326.0)
        assert ctx["slippage_r"] == pytest.approx(0.50 / 0.98, rel=1e-3)
        assert ctx["slippage_rs"] == pytest.approx(0.5 * 930)
        assert (ctx["entry_order_id"], ctx["exit_order_id"]) == ("ENTRY-1", "CLOSE-1")
        assert "partial" not in ctx
        # the ledger reads context["reason"] as a short label; the sentence lives in "detail"
        assert ctx["reason"] == SLIPPAGE
        assert "slipped" in ctx["detail"]

    async def test_the_loss_is_in_the_day_total_under_its_own_exit_type(self):
        await _establish()
        trades = db.fetch_day_trades("analyze")
        assert len(trades) == 1
        assert trades[0]["total_pnl"] == pytest.approx(-93.0)
        assert trades[0]["exit_types"] == [BAD_FILL]
        assert trades[0]["exit_class"] == BAD_FILL
        assert trades[0]["execution_exit"] is True

    async def test_a_normal_strategy_exit_is_not_flagged_as_an_execution_exit(self):
        db.save_tracker_exit(
            strategy="BREAKOUT-PY", symbol="PFC", entry=100.0, sl=99.0, tp=102.0, quantity=1,
            exit_price=102.0, pnl=2.0, exit_types=["TP1"],
        )
        row = db.fetch_day_trades("analyze")[0]
        assert row["execution_exit"] is False
        assert row["exit_class"] == "STRATEGY_EXIT"

    async def test_a_fill_past_tp_has_its_own_type(self):
        established, _ = await _establish(_signal(tp=326.00), max_slip_r=99.0)
        assert established is False
        assert db.fetch_day_trades("analyze")[0]["exit_types"] == [BAD_FILL]
        assert json.loads(_rows()[-1][7])["reason"] == TP_OVERSHOT

    async def test_without_the_exit_fill_the_broker_realised_delta_is_booked(self):
        await _establish(close_fill=None, realised_delta=-90.0)
        assert db.fetch_day_trades("analyze")[0]["total_pnl"] == pytest.approx(-90.0)

    async def test_the_risk_engine_receives_the_real_loss(self):
        """Not 0.0: the loss is real and must reach the daily-loss counters."""
        _, mocks = await _establish()
        mocks.risk.record_close.assert_called_once()
        assert mocks.risk.record_close.call_args.args[0] == pytest.approx(-93.0)
        assert mocks.risk.record_close.call_args.kwargs == {
            "strategy": "BREAKOUT-PY", "symbol": "PFC",
        }


@pytest.mark.asyncio
class TestBadFillLossReachesTheDailyLossLimit:
    """The loss is real, so it counts toward the daily limit and can stop further trading."""

    @staticmethod
    def _engine(limit):
        from signal_engine.tests.risk_fixtures import _engine

        engine = _engine(daily_loss_limit=limit, max_open_positions=5)
        engine._state("BREAKOUT-PY").last_known_capital = 100_000.0
        return engine

    async def test_a_bad_fill_loss_at_the_limit_blocks_the_next_entry(self):
        engine = self._engine(limit=0.0009)  # 90.0 of 100,000; the PFC loss is 93.0
        assert engine.check_exposure("BREAKOUT-PY") is True
        await _establish(risk=engine)
        state = engine._state("BREAKOUT-PY")
        assert state.daily_realised_loss == pytest.approx(93.0)
        assert state.daily_net_pnl == pytest.approx(-93.0)
        assert engine.check_exposure("BREAKOUT-PY") is False
        assert "Daily loss limit" in engine.exposure_block_reason("BREAKOUT-PY")

    async def test_a_bad_fill_loss_below_the_limit_does_not_block(self):
        engine = self._engine(limit=0.01)  # 1,000 of 100,000
        await _establish(risk=engine)
        assert engine._state("BREAKOUT-PY").daily_realised_loss == pytest.approx(93.0)
        assert engine.check_exposure("BREAKOUT-PY") is True

    async def test_the_loss_lands_on_the_strategy_that_took_the_fill(self):
        engine = self._engine(limit=0.0009)
        engine._state("BREAKOUT").last_known_capital = 100_000.0
        await _establish(risk=engine)
        assert engine.check_exposure("BREAKOUT") is True  # analyze mode isolates strategies

    async def test_the_ledger_and_the_risk_counter_record_the_same_loss(self):
        engine = self._engine(limit=0.01)
        await _establish(risk=engine)
        ledger = db.fetch_day_trades("analyze")[0]["total_pnl"]
        assert engine._state("BREAKOUT-PY").daily_realised_loss == pytest.approx(-ledger)


@pytest.mark.asyncio
class TestCloseOrderFailureNeverLeavesAnUntrackedPosition:
    async def test_failed_close_with_position_still_open_restores_stop_and_tracks_it(self):
        failed = TradeResult(status=OrderStatus.REJECTED, order_id="", message="margin")
        established, mocks = await _establish(close_result=failed, broker_qty=930)
        assert established is True  # carried on as a normal tracked entry
        assert mocks.place_bracket.await_count == 2  # the stop was cancelled, so it is restored
        mocks.notifier.notify_exit_failed.assert_awaited_once()
        mocks.tracker.register.assert_called_once()
        assert mocks.tracker.register.call_args.args[0].sl_order_id == "SL-2"
        assert _rows() == []  # nothing is booked as closed
        assert mocks.risk.record_close.call_count == 0

    async def test_when_the_stop_cancel_failed_no_second_stop_is_placed(self):
        """The first stop is still live. A second full-size SL-M would give two stops, and if
        both triggered the second would open a naked short."""
        failed = TradeResult(status=OrderStatus.REJECTED, order_id="", message="margin")
        established, mocks = await _establish(
            close_result=failed, broker_qty=930, cancel_ok=False
        )
        assert established is True
        assert mocks.place_bracket.await_count == 1  # only the original entry stop
        assert mocks.tracker.register.call_args.args[0].sl_order_id == "SL-1"

    async def test_the_failure_alert_does_not_claim_the_position_was_closed(self):
        failed = TradeResult(status=OrderStatus.REJECTED, order_id="", message="margin")
        _, mocks = await _establish(close_result=failed, broker_qty=930)
        text = mocks.notifier.notify_exit_failed.await_args.args[1]
        assert "auto-close attempted" in text and "auto-closed" not in text

    async def test_a_timed_out_close_that_filled_is_found_in_the_orderbook(self):
        """A close the broker accepted but has not filled yet reads as open for a moment. The
        recent-fill lookup finds it, so no stop is restored and the ledger gets the real id."""
        timeout = TradeResult(status=OrderStatus.TIMEOUT, order_id="", message="slow")
        found = TradeResult(status=OrderStatus.SUCCESS, order_id="CLOSE-FOUND")
        established, mocks = await _establish(
            close_result=timeout, broker_qty=930, recent_fill=found
        )
        assert established is False
        assert mocks.place_bracket.await_count == 1
        exit_row = _rows()[-1]
        assert exit_row[1] == "EXIT" and exit_row[3] == "CLOSE-FOUND"

    async def test_unreachable_broker_is_treated_as_still_open(self):
        failed = TradeResult(status=OrderStatus.ERROR, order_id="", message="boom")
        established, mocks = await _establish(close_result=failed, broker_qty=None)
        assert established is True
        mocks.tracker.register.assert_called_once()

    async def test_timeout_that_actually_filled_is_booked_as_closed_without_a_new_stop(self):
        """A restored stop on an already-flat position would open a NEW short."""
        timeout = TradeResult(status=OrderStatus.TIMEOUT, order_id="", message="slow")
        established, mocks = await _establish(close_result=timeout, broker_qty=0)
        assert established is False
        assert mocks.place_bracket.await_count == 1  # only the original entry stop
        assert [r[1] for r in _rows()] == ["LONG", "EXIT"]
        mocks.tracker.register.assert_not_called()
