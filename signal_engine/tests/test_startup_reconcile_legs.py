"""E-13 / R-03: restart reconciliation with partial legs and with two strategies on one symbol."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import db
from signal_engine.models import Action, Direction, Order, OrderStatus, TradeResult
from signal_engine.startup import reconcile_open_positions
from signal_engine.tests.conftest import make_signal
from signal_engine.tests.risk_fixtures import _engine
from signal_engine.tests.test_startup_reconciliation import _local_position, _tracker


def _order(strategy):
    return Order(
        symbol="SBIN", exchange="NSE", action=Action.BUY, quantity=10, price=0.0,
        order_type="MARKET", product="MIS", strategy_tag=strategy,
    )


def _save(strategy, direction, context=None, oid="O1", message="success"):
    sig = make_signal(strategy=strategy, symbol="SBIN", direction=direction)
    if context is not None:
        sig.context = context
    db.save(sig, _order(strategy), TradeResult(order_id=oid, status=OrderStatus.SUCCESS, message=message))


class TestOpenPositionsView:
    def test_a_partial_leg_does_not_hide_the_open_position(self):
        _save("BREAKOUT", Direction.LONG)
        _save("BREAKOUT", Direction.EXIT, {"pnl": 500.0, "exit_types": ["TP1"], "partial": True})
        open_positions = db.fetch_all_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0]["legs_pnl"] == 500.0

    def test_a_closing_exit_still_closes_it(self):
        _save("BREAKOUT", Direction.LONG)
        _save("BREAKOUT", Direction.EXIT, {"pnl": 500.0, "exit_types": ["TP1"], "partial": True})
        _save("BREAKOUT", Direction.EXIT, {"pnl": 400.0, "exit_types": ["SL"]}, message="Tracker close")
        assert db.fetch_all_open_positions() == []


def _flat_row(pnl):
    return {
        "symbol": "HINDALCO", "quantity": 0, "product": "MIS",
        "today_realized_pnl": pnl, "pnl": pnl, "ltp": 1019.4,
    }


async def _reconcile(local, engine):
    with (
        patch("signal_engine.db.fetch_all_open_positions", return_value=local),
        patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock(return_value=[_flat_row(-100.0)])),
        patch("signal_engine.db.save_reconciled_exit") as save,
    ):
        await reconcile_open_positions(engine, _tracker())
    return save


@pytest.mark.asyncio
async def test_risk_books_only_what_the_partial_legs_did_not_already_cover():
    engine = _engine()
    engine._state("BREAKOUT").open_positions = 1
    # broker total for the symbol is -100: a +500 leg is already booked, so the runner lost -600
    save = await _reconcile([{**_local_position(), "legs_pnl": 500.0}], engine)
    assert save.call_args[0][7] == -100.0  # the ledger row keeps the broker's full figure
    assert engine._state("BREAKOUT").daily_net_pnl == -600.0


@pytest.mark.asyncio
async def test_two_strategies_on_one_flat_symbol_book_nothing_and_log_critical(logged):
    engine = _engine()
    engine._state("BREAKOUT").open_positions = 1
    local = [
        {**_local_position(), "legs_pnl": 0.0},
        {**_local_position(strategy="BREAKINGTRADE"), "legs_pnl": 0.0},
    ]
    save = await _reconcile(local, engine)
    save.assert_not_called()
    assert engine._state("BREAKOUT").daily_net_pnl == 0.0
    assert logged.has("at least one other strategy both show it open", "CRITICAL")
