"""Position tracker — polls OpenAlgo to detect closed positions and update risk counters."""

import asyncio
from dataclasses import dataclass
from typing import Dict

from loguru import logger

from signal_engine.api_client import cancel_order, fetch_open_position, fetch_realised_pnl
from signal_engine.risk import RiskEngine


@dataclass
class TrackedPosition:
    symbol: str
    strategy: str
    exchange: str
    product: str
    entry_price: float
    quantity: int
    sl: float
    tp: float
    entry_order_id: str = ""
    sl_order_id: str = ""
    tp_order_id: str = ""


class PositionTracker:
    """Monitors open positions by polling OpenAlgo API.

    When a position closes (quantity drops to 0), it computes approximate P&L
    and updates the risk engine's loss counters.
    """

    def __init__(self, risk_engine: RiskEngine, poll_interval: int = 30):
        self._positions: Dict[str, TrackedPosition] = {}
        self._risk_engine = risk_engine
        self._poll_interval = poll_interval
        self._running = False
        self._last_realised_pnl: float = 0.0

    @property
    def tracked_count(self) -> int:
        return len(self._positions)

    def register(self, position: TrackedPosition) -> None:
        """Register a new position to track."""
        key = f"{position.symbol}:{position.strategy}"
        self._positions[key] = position
        logger.info(f"Tracking position: {key} qty={position.quantity}")

    async def check_positions(self) -> None:
        """Poll all tracked positions once and process closures."""
        closed_keys = []

        for key, pos in self._positions.items():
            qty = await fetch_open_position(
                symbol=pos.symbol,
                strategy=pos.strategy,
                exchange=pos.exchange,
                product=pos.product,
            )

            if qty == -1:
                # API error, skip this cycle
                continue

            if qty == 0:
                # Position closed — estimate P&L from realised PnL delta
                current_realised = await fetch_realised_pnl()
                pnl_delta = current_realised - self._last_realised_pnl
                self._last_realised_pnl = current_realised

                self._risk_engine.record_close(pnl_delta)
                self._risk_engine.remove_margin(
                    qty=pos.quantity,
                    entry_price=pos.entry_price,
                    product=pos.product,
                )
                logger.info(f"Position closed: {key}, PnL delta: {pnl_delta:,.2f}")

                # OCO cancellation: cancel whichever bracket leg is still pending
                await self._cancel_remaining_bracket_leg(pos, pnl_delta)

                closed_keys.append(key)

        for key in closed_keys:
            del self._positions[key]

    async def _cancel_remaining_bracket_leg(self, pos: "TrackedPosition", pnl_delta: float) -> None:
        """Cancel whichever bracket leg is still pending after position closes.

        If pnl_delta < 0, SL was likely triggered -> cancel TP.
        If pnl_delta >= 0, TP was likely triggered -> cancel SL.
        If bracket IDs are not set, nothing to cancel.
        """
        if not pos.sl_order_id or not pos.tp_order_id:
            return

        # Determine which leg to cancel based on P&L direction
        if pnl_delta < 0:
            # Loss: SL was triggered, cancel the pending TP
            leg_to_cancel = pos.tp_order_id
            trigger_leg = "SL"
            cancel_leg = "TP"
        else:
            # Profit (or breakeven): TP was triggered, cancel the pending SL
            leg_to_cancel = pos.sl_order_id
            trigger_leg = "TP"
            cancel_leg = "SL"

        logger.info(
            f"Position {pos.symbol}:{pos.strategy} closed by {trigger_leg} trigger, "
            f"cancelling remaining {cancel_leg} order {leg_to_cancel}"
        )

        try:
            success = await cancel_order(leg_to_cancel, pos.strategy)
            if success:
                logger.info(f"Cancelled {cancel_leg} order {leg_to_cancel} for {pos.symbol}")
            else:
                logger.warning(f"Failed to cancel {cancel_leg} order {leg_to_cancel} for {pos.symbol}")
        except Exception as e:
            logger.error(f"Exception cancelling {cancel_leg} order {leg_to_cancel} for {pos.symbol}: {e}")

    async def start(self) -> None:
        """Start the polling loop. Runs until stop() is called."""
        self._running = True
        # Snapshot starting realised PnL
        self._last_realised_pnl = await fetch_realised_pnl()
        logger.info(f"Position tracker started (poll every {self._poll_interval}s)")

        while self._running:
            if self._positions:
                await self.check_positions()
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._running = False
        logger.info("Position tracker stopped")
