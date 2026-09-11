"""The LIVE (pooled) branch of startup reconciliation — previously untested (M11).

In LIVE every strategy name resolves to the same shared counter bucket (RiskEngine._key), so
the counter must be corrected ONCE against the broker's TOTAL. Correcting it per strategy
would overwrite the shared total with each strategy's own partial count in turn, last one
winning.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import startup


def _risk(isolates: bool, stored: int, known=None):
    risk = MagicMock()
    risk.isolates_per_strategy = isolates
    risk.total_open_positions.return_value = stored
    risk.open_positions_for.return_value = stored
    # Reconciliation unions this in so idle strategies get corrected too (M6).
    risk.known_strategies.return_value = set(known or ())
    return risk


def _tracker():
    tracker = MagicMock()
    tracker.find_position.return_value = None
    return tracker


def _position(symbol, qty=100, product="MIS"):
    return {"symbol": symbol, "quantity": qty, "product": product, "average_price": 800.0}


@pytest.fixture(autouse=True)
def _no_network():
    with patch("signal_engine.startup._recover_sl_order_ids", AsyncMock(return_value={})), \
         patch("signal_engine.startup._restore_tracker_positions", return_value=0):
        yield


class TestLivePooledCorrection:
    @pytest.mark.asyncio
    async def test_corrects_the_shared_counter_once_against_the_broker_total(self):
        risk = _risk(isolates=False, stored=1)
        positions = [_position("SBIN"), _position("TCS")]
        with patch("signal_engine.db.fetch_all_open_positions", return_value=[]), \
             patch("signal_engine.api_client.fetch_positionbook", AsyncMock(return_value=positions)):
            await startup.reconcile_open_positions(risk, _tracker())
        risk.set_open_positions.assert_called_once()
        assert risk.set_open_positions.call_args.args[1] == 2

    @pytest.mark.asyncio
    async def test_a_matching_counter_is_left_alone(self):
        risk = _risk(isolates=False, stored=1)
        with patch("signal_engine.db.fetch_all_open_positions", return_value=[]), \
             patch("signal_engine.api_client.fetch_positionbook",
                   AsyncMock(return_value=[_position("SBIN")])):
            await startup.reconcile_open_positions(risk, _tracker())
        risk.set_open_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_open_anywhere_is_a_no_op(self):
        risk = _risk(isolates=False, stored=0)
        with patch("signal_engine.db.fetch_all_open_positions", return_value=[]), \
             patch("signal_engine.api_client.fetch_positionbook", AsyncMock()) as book:
            await startup.reconcile_open_positions(risk, _tracker())
        book.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreachable_positionbook_changes_nothing(self):
        """Correcting counters against an answer we do not have would be worse than
        leaving them where the persisted state says."""
        risk = _risk(isolates=False, stored=2)
        with patch("signal_engine.db.fetch_all_open_positions", return_value=[]), \
             patch("signal_engine.api_client.fetch_positionbook", AsyncMock(return_value=None)):
            await startup.reconcile_open_positions(risk, _tracker())
        risk.set_open_positions.assert_not_called()


class TestAnalyzePerStrategyCorrection:
    @pytest.mark.asyncio
    async def test_each_strategy_is_corrected_against_its_own_count(self):
        risk = _risk(isolates=True, stored=0)
        risk.open_positions_for.side_effect = lambda s: 0
        locally_open = [
            {"symbol": "SBIN", "strategy": "ORB", "entry": 800.0, "sl": 796.0,
             "tp": 810.0, "quantity": 100},
            {"symbol": "TCS", "strategy": "BREAKOUT", "entry": 4000.0, "sl": 3980.0,
             "tp": 4050.0, "quantity": 10},
        ]
        positions = [_position("SBIN"), _position("TCS", qty=10)]
        with patch("signal_engine.db.fetch_all_open_positions", return_value=locally_open), \
             patch("signal_engine.api_client.fetch_positionbook", AsyncMock(return_value=positions)):
            await startup.reconcile_open_positions(risk, _tracker())
        corrected = {c.args[0]: c.args[1] for c in risk.set_open_positions.call_args_list}
        assert corrected == {"ORB": 1, "BREAKOUT": 1}
