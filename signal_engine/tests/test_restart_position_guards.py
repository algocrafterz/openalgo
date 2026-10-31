"""Positions held across a restart: the per-symbol cap and the legs another strategy hides.

BEL 2026-10-28: the symbol counts are in memory only, so after the 11:06 restart EVWAP re-entered
a name it still held; the second entry replaced the first in the tracker and the duplicate-stop
cleanup cancelled the first leg's stop. HDFCBANK the same day: EVWAP's stopped-out short was
hidden behind BREAKINGTRADE's long, so it was neither restored nor booked (-1,049).
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import startup
from signal_engine.tests.conftest import make_signal
from signal_engine.tests.risk_fixtures import _engine
from signal_engine.timeutils import IST


class TestSeedConcentration:
    def test_a_restored_position_blocks_a_second_entry_on_the_symbol(self):
        engine = _engine(max_positions_per_symbol=1)
        assert engine.can_trade_symbol("BEL", "EVWAP-EARLY-PY")
        engine.seed_concentration([("EVWAP-EARLY-PY", "BEL")])
        assert not engine.can_trade_symbol("BEL", "EVWAP-EARLY-PY")
        assert engine.can_trade_symbol("BEL", "BREAKINGTRADE")  # another strategy may take it

    def test_seeding_twice_never_double_counts(self):
        engine = _engine(max_positions_per_symbol=1)
        engine.seed_concentration([("EVWAP-EARLY-PY", "BEL")])
        engine.seed_concentration([("EVWAP-EARLY-PY", "BEL")])
        engine.record_close(0.0, strategy="EVWAP-EARLY-PY", symbol="BEL")
        assert engine.can_trade_symbol("BEL", "EVWAP-EARLY-PY")


@pytest.mark.asyncio
class TestEntryGateOnATrackedSymbol:
    async def test_a_strategy_already_tracking_the_symbol_is_declined(self):
        from signal_engine import main

        signal = make_signal(strategy="EVWAP-EARLY-PY", symbol="BEL")
        tracker = MagicMock(find_position=MagicMock(return_value=object()))
        risk = MagicMock(check_exposure=MagicMock(return_value=True))
        with (
            patch.object(main, "tracker", tracker),
            patch.object(main, "risk_engine", risk),
            patch.object(main, "_decline", AsyncMock()) as decline,
        ):
            assert await main._entry_passes_risk_gates(signal) is False
        assert decline.await_args.kwargs["reason"] == "symbol already held by this strategy"
        risk.can_trade_symbol.assert_not_called()


def _hdfcbank_leg():
    return {
        "strategy": "EVWAP-EARLY-PY", "symbol": "HDFCBANK", "direction": "SHORT",
        "entry": 674.55, "sl": 678.6, "tp": 0.0, "quantity": 259, "order_id": "26102811541218",
        "executed_at": "2026-10-28T10:20:05", "sig_id": "S1", "fill_price": 674.55,
    }


def _stop_fill_row(strategy="EVWAP-EARLY-PY", qty=259):
    return {"orderid": "26102883531763", "symbol": "HDFCBANK", "action": "BUY", "quantity": qty,
            "order_status": "complete", "timestamp": "2026-10-28 11:05:21", "strategy": strategy}


async def _book(rows, fill=678.6):
    tracker = MagicMock(find_position=MagicMock(return_value=None))
    risk = MagicMock()
    with (
        patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=rows)),
        patch("signal_engine.api_client.fetch_order_fill_price", AsyncMock(return_value=fill)),
        patch("signal_engine.db.save_reconciled_exit") as save,
    ):
        await startup._book_legs_hidden_by_another_strategy(
            [_hdfcbank_leg()], {"HDFCBANK"}, tracker, risk
        )
    return save, risk


@pytest.mark.asyncio
class TestLegHiddenByAnotherStrategy:
    async def test_its_own_stop_fill_is_booked_with_the_real_order(self):
        save, risk = await _book([_stop_fill_row()])
        args, kwargs = save.call_args
        assert args[:2] == ("EVWAP-EARLY-PY", "HDFCBANK")
        assert args[6] == 678.6
        assert args[7] == pytest.approx((674.55 - 678.6) * 259)  # -1,048.95
        assert kwargs["order_id"] == "26102883531763"
        risk.record_close.assert_called_once()

    async def test_another_strategys_order_is_never_taken_as_its_close(self):
        save, risk = await _book([_stop_fill_row(strategy="BREAKINGTRADE")])
        save.assert_not_called()
        risk.record_close.assert_not_called()

    async def test_no_closing_order_is_alerted_not_guessed(self):
        save, _ = await _book([])
        save.assert_not_called()


def test_executed_at_is_read_as_ist():
    assert startup._executed_at_ist("2026-10-28T10:20:05") == datetime(2026, 10, 28, 10, 20, 5, tzinfo=IST)
