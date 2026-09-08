"""Position reconciliation at startup (startup.reconcile_open_positions).

The gap this locks in: the ORIGINAL function only ever walked the broker's currently-NONZERO
positions to (a) restore the in-memory tracker and (b) correct risk_engine.open_positions if the
COUNT was wrong. A position that closed while the engine was down never appears in that list at
all - so nothing about it was ever detected, trades.db kept showing it open forever, and the
realised-loss counter never saw its P&L. HINDALCO, 2026-09-08.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.startup import reconcile_open_positions
from signal_engine.tests.risk_fixtures import _engine


def _engine_with_open(open_positions: int, **overrides):
    """_engine() builds a RiskEngine with fresh counters; open_positions is runtime state set
    after construction (normally restored from RiskStore), not a constructor argument."""
    engine = _engine(**overrides)
    engine.open_positions = open_positions
    return engine


def _local_position(**overrides) -> dict:
    defaults = {
        "strategy": "BREAKOUT",
        "symbol": "HINDALCO",
        "direction": "LONG",
        "entry": 1022.4,
        "sl": 1019.43,
        "tp": 1026.85,
        "quantity": 107,
        "order_id": "abc123",
        "executed_at": "2026-09-08 05:20:07",
    }
    defaults.update(overrides)
    return defaults


class TestClosedWhileDown:
    """The new behaviour: a locally-open position the broker already flattened."""

    @pytest.mark.asyncio
    async def test_reconciles_a_position_closed_while_the_engine_was_down(self):
        risk_engine = _engine_with_open(1)
        tracker = AsyncMock()
        closed_row = {
            "symbol": "HINDALCO", "quantity": 0, "product": "MIS",
            "today_realized_pnl": -363.8, "pnl": -363.8, "ltp": 1019.4,
        }

        with (
            patch("signal_engine.db.fetch_all_open_positions", return_value=[_local_position()]),
            patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock(return_value=[closed_row])),
            patch("signal_engine.db.save_reconciled_exit") as mock_save,
            patch("signal_engine.notifier.notify_position_closed", new=AsyncMock()) as mock_notify,
        ):
            await reconcile_open_positions(risk_engine, tracker)

        mock_save.assert_called_once()
        args = mock_save.call_args[0]
        assert args[0] == "BREAKOUT"
        assert args[1] == "HINDALCO"
        assert args[6] == pytest.approx(1019.4)  # fill_price positional arg
        assert args[7] == pytest.approx(-363.8)  # pnl positional arg

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["strategy"] == "BREAKOUT"

    @pytest.mark.asyncio
    async def test_updates_the_realised_loss_counter(self):
        """The actual bug: not just "an exit gets recorded" but "risk tracking sees the loss"."""
        risk_engine = _engine_with_open(1)
        tracker = AsyncMock()
        closed_row = {
            "symbol": "HINDALCO", "quantity": 0, "product": "MIS",
            "today_realized_pnl": -363.8, "ltp": 1019.4,
        }

        with (
            patch("signal_engine.db.fetch_all_open_positions", return_value=[_local_position()]),
            patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock(return_value=[closed_row])),
            patch("signal_engine.db.save_reconciled_exit"),
            patch("signal_engine.notifier.notify_position_closed", new=AsyncMock()),
        ):
            await reconcile_open_positions(risk_engine, tracker)

        assert risk_engine.daily_realised_loss == pytest.approx(363.8)
        assert risk_engine.open_positions == 0

    @pytest.mark.asyncio
    async def test_runs_even_when_stored_open_positions_is_already_zero(self):
        """The regression this fixes directly: the old early-return (`if open_positions <= 0:
        return`) would have skipped reconciliation entirely here, even though trades.db has a
        real open position the broker has already closed."""
        risk_engine = _engine_with_open(0)
        tracker = AsyncMock()
        closed_row = {
            "symbol": "HINDALCO", "quantity": 0, "product": "MIS",
            "today_realized_pnl": -363.8, "ltp": 1019.4,
        }

        with (
            patch("signal_engine.db.fetch_all_open_positions", return_value=[_local_position()]),
            patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock(return_value=[closed_row])),
            patch("signal_engine.db.save_reconciled_exit") as mock_save,
            patch("signal_engine.notifier.notify_position_closed", new=AsyncMock()),
        ):
            await reconcile_open_positions(risk_engine, tracker)

        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_still_genuinely_open_position_is_left_to_the_restore_path(self):
        """A position that IS still open at the broker must not be treated as closed."""
        risk_engine = _engine_with_open(1)
        tracker = AsyncMock()
        still_open_row = {"symbol": "HINDALCO", "quantity": 107, "product": "MIS", "ltp": 1030.0}

        with (
            patch("signal_engine.db.fetch_all_open_positions", return_value=[_local_position()]),
            patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock(return_value=[still_open_row])),
            patch("signal_engine.db.save_reconciled_exit") as mock_save,
            patch("signal_engine.db.fetch_last_entry_trade", return_value=None),
        ):
            await reconcile_open_positions(risk_engine, tracker)

        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_symbol_missing_from_positionbook_entirely_is_left_alone(self):
        """No broker record at all (not even a flat one) - nothing to reconcile from, so this
        must not guess a P&L. Logs a warning and moves on."""
        risk_engine = _engine_with_open(1)
        tracker = AsyncMock()

        with (
            patch("signal_engine.db.fetch_all_open_positions", return_value=[_local_position()]),
            patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock(return_value=[])),
            patch("signal_engine.db.save_reconciled_exit") as mock_save,
        ):
            await reconcile_open_positions(risk_engine, tracker)

        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_local_and_nothing_stored_is_a_true_no_op(self):
        risk_engine = _engine_with_open(0)
        tracker = AsyncMock()

        with (
            patch("signal_engine.db.fetch_all_open_positions", return_value=[]),
            patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock()) as mock_fetch,
        ):
            await reconcile_open_positions(risk_engine, tracker)

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_positionbook_unreachable_skips_without_crashing(self):
        risk_engine = _engine_with_open(1)
        tracker = AsyncMock()

        with (
            patch("signal_engine.db.fetch_all_open_positions", return_value=[_local_position()]),
            patch("signal_engine.api_client.fetch_positionbook", new=AsyncMock(return_value=None)),
            patch("signal_engine.db.save_reconciled_exit") as mock_save,
        ):
            await reconcile_open_positions(risk_engine, tracker)

        mock_save.assert_not_called()
