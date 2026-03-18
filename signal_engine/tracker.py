"""Position tracker — polls OpenAlgo to detect closed positions and update risk counters."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, Set

from loguru import logger

from signal_engine.api_client import (
    cancel_all_orders,
    cancel_order,
    close_all_positions,
    fetch_positionbook,
    fetch_realised_pnl,
)
from signal_engine.risk import RiskEngine

# IST offset from UTC
_IST = timezone(timedelta(hours=5, minutes=30))


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
        """Poll all tracked positions with a single positionbook call."""
        # Single API call for all positions
        book = await fetch_positionbook()
        if book is None:
            # API error, skip entire cycle
            return

        # Build lookup: symbol -> quantity from positionbook
        book_qty = {}
        for entry in book:
            sym = entry.get("symbol", "")
            qty = int(entry.get("quantity", 0))
            book_qty[sym] = qty

        closed_keys = []

        for key, pos in self._positions.items():
            qty = book_qty.get(pos.symbol, 0)

            if qty != 0:
                continue

            # Position closed — estimate P&L from realised PnL delta
            current_realised = await fetch_realised_pnl()
            pnl_delta = current_realised - self._last_realised_pnl
            self._last_realised_pnl = current_realised

            self._risk_engine.record_close(pnl_delta, symbol=pos.symbol)
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

    async def time_exit_all(self) -> None:
        """Force-close all tracked positions and cancel all pending bracket orders.

        Called by the TimeExitScheduler at the configured time (e.g., 15:00 IST).
        Uses strategy-level close/cancel APIs for reliability.
        """
        if not self._positions:
            logger.info("Time exit: no open positions to close")
            return

        # Collect unique strategies from tracked positions
        strategies: Set[str] = {pos.strategy for pos in self._positions.values()}

        for strategy in strategies:
            # Cancel all pending orders first (SL-M + TP LIMIT legs)
            logger.info(f"Time exit: cancelling all pending orders for strategy={strategy}")
            await cancel_all_orders(strategy)

            # Close all open positions
            logger.info(f"Time exit: closing all positions for strategy={strategy}")
            await close_all_positions(strategy)

        # Clear tracked positions and update risk engine
        for key, pos in list(self._positions.items()):
            self._risk_engine.record_close(pnl=0.0, symbol=pos.symbol)
            self._risk_engine.remove_margin(
                qty=pos.quantity,
                entry_price=pos.entry_price,
                product=pos.product,
            )
            logger.info(f"Time exit: cleared tracker entry {key}")

        self._positions.clear()

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


class TimeExitScheduler:
    """Schedules a one-shot time exit at a fixed IST time each trading day.

    Checks the clock every 30 seconds. When the configured time (e.g., 15:00 IST)
    is reached, calls tracker.time_exit_all() to close positions and cancel orders.
    Fires once per day — resets at midnight IST.
    """

    def __init__(self, tracker: PositionTracker, hour: int, minute: int):
        self._tracker = tracker
        self._hour = hour
        self._minute = minute
        self._running = False
        self._fired_today: bool = False
        self._last_date = datetime.now(_IST).date()

    async def start(self) -> None:
        self._running = True
        logger.info(f"Time exit scheduler started: {self._hour:02d}:{self._minute:02d} IST")

        while self._running:
            now = datetime.now(_IST)

            # Reset fired flag on new day
            if now.date() != self._last_date:
                self._fired_today = False
                self._last_date = now.date()

            # Check if it's time to exit
            if (
                not self._fired_today
                and now.hour == self._hour
                and now.minute >= self._minute
            ):
                logger.info(
                    f"Time exit triggered at {now.strftime('%H:%M')} IST "
                    f"(configured: {self._hour:02d}:{self._minute:02d})"
                )
                await self._tracker.time_exit_all()
                self._fired_today = True

            # Also fire if past the configured time (in case scheduler started late)
            if (
                not self._fired_today
                and (now.hour > self._hour or (now.hour == self._hour and now.minute > self._minute + 5))
            ):
                logger.info(f"Time exit: past configured time, firing catch-up exit")
                await self._tracker.time_exit_all()
                self._fired_today = True

            await asyncio.sleep(30)

    def stop(self) -> None:
        self._running = False
        logger.info("Time exit scheduler stopped")
