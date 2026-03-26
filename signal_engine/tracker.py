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
from signal_engine.config import settings
from signal_engine.executor import build_exit_order, send_order
from signal_engine.models import Action, Direction, Order, OrderStatus
from signal_engine.risk import RiskEngine
from signal_engine import notifier

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
    direction: Direction = Direction.LONG
    entry_order_id: str = ""
    sl_order_id: str = ""
    tp_order_id: str = ""  # unused: TP handled via LTP monitoring, not broker order
    tp_triggered: bool = False  # guard against double-exit
    tp_monitoring: bool = True  # False for swing strategies (exit via PineScript EXIT signal)


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
        # Day summary counters (reset at midnight via TimeExitScheduler)
        self._day_trades: int = 0
        self._day_wins: int = 0
        self._day_losses: int = 0
        self._day_pnl: float = 0.0

    @property
    def tracked_count(self) -> int:
        return len(self._positions)

    def register(self, position: TrackedPosition) -> None:
        """Register a new position to track."""
        key = f"{position.symbol}:{position.strategy}"
        self._positions[key] = position
        logger.info(f"Tracking position: {key} qty={position.quantity}")

    def find_position(self, symbol: str, strategy: str) -> "TrackedPosition | None":
        """Look up a tracked position by symbol and strategy. Returns None if not found."""
        key = f"{symbol}:{strategy}"
        return self._positions.get(key)

    def unregister(self, symbol: str, strategy: str) -> "TrackedPosition | None":
        """Remove and return a tracked position. Returns None if not found."""
        key = f"{symbol}:{strategy}"
        pos = self._positions.pop(key, None)
        if pos:
            logger.info(f"Unregistered position: {key}")
        return pos

    async def check_positions(self) -> None:
        """Poll all tracked positions with a single positionbook call.

        Two things are checked each cycle:
        1. TP monitoring: if LTP has crossed the TP price, cancel SL and exit at market.
        2. Close detection: if position qty == 0, update risk counters.
        """
        book = await fetch_positionbook()
        if book is None:
            return

        # Build lookup: symbol -> (quantity, ltp)
        book_data: dict = {}
        for entry in book:
            sym = entry.get("symbol", "")
            qty = int(entry.get("quantity", 0))
            ltp = float(entry.get("ltp", 0) or 0)
            book_data[sym] = (qty, ltp)

        closed_keys = []

        for key, pos in self._positions.items():
            qty, ltp = book_data.get(pos.symbol, (0, 0.0))

            # --- TP monitoring: exit at market when TP level is reached ---
            # Skip for positions with tp_monitoring=False (swing strategies use EXIT signals)
            if qty != 0 and pos.tp_monitoring and not pos.tp_triggered and ltp > 0:
                tp_hit = (
                    (pos.direction == Direction.LONG and ltp >= pos.tp)
                    or (pos.direction == Direction.SHORT and ltp <= pos.tp)
                )
                if tp_hit:
                    logger.info(
                        f"TP level reached for {pos.symbol}: ltp={ltp} tp={pos.tp} — cancelling SL and exiting at market"
                    )
                    pos.tp_triggered = True
                    await notifier.notify_tp_level_hit(pos.symbol, ltp, pos.tp, strategy=pos.strategy)
                    await self._exit_at_tp(pos)
                    # Position will close; detected as qty==0 in a future cycle
                    continue

            if qty != 0:
                continue

            # --- Position closed ---
            current_realised = await fetch_realised_pnl()
            pnl_delta = current_realised - self._last_realised_pnl
            self._last_realised_pnl = current_realised

            self._risk_engine.record_close(pnl_delta, symbol=pos.symbol)
            logger.info(f"Position closed: {key}, PnL delta: {pnl_delta:,.2f}")

            self._day_trades += 1
            self._day_pnl += pnl_delta
            if pnl_delta >= 0:
                self._day_wins += 1
            else:
                self._day_losses += 1

            await notifier.notify_position_closed(pos.symbol, pnl_delta, strategy=pos.strategy)

            # If SL was triggered (position closed without TP trigger), cancel any
            # pending SL order (it should already be gone, but guard against edge cases)
            if not pos.tp_triggered and pos.sl_order_id:
                # SL-M self-cancels when triggered; this is a no-op in most cases
                pass

            closed_keys.append(key)

        for key in closed_keys:
            del self._positions[key]

    async def _exit_at_tp(self, pos: "TrackedPosition") -> None:
        """Cancel the pending SL order and place a market exit at TP.

        Called when LTP crosses the TP price. The SL order must be cancelled first
        so the market exit is recognised as position closure, not a double-short.
        """
        # Cancel SL first to free the position for a clean exit
        if pos.sl_order_id:
            success = await cancel_order(pos.sl_order_id, pos.strategy)
            if success:
                logger.info(f"SL order {pos.sl_order_id} cancelled before TP market exit for {pos.symbol}")
            else:
                logger.warning(f"Failed to cancel SL {pos.sl_order_id} for {pos.symbol} — proceeding with market exit anyway")
                await notifier.notify_sl_cancel_failed(pos.symbol, pos.sl_order_id, strategy=pos.strategy)

        # Place market exit order with retries
        exit_order = build_exit_order(
            symbol=pos.symbol,
            exchange=pos.exchange,
            quantity=pos.quantity,
            product=pos.product,
            strategy_tag=pos.strategy,
        )
        max_attempts = settings.bracket_tp_exit_retries
        result = None
        for attempt in range(1, max_attempts + 1):
            result = await send_order(exit_order)
            if result.status == OrderStatus.SUCCESS:
                logger.info(f"TP market exit placed for {pos.symbol}: id={result.order_id} (attempt {attempt})")
                await notifier.notify_tp_exit_placed(pos.symbol, result.order_id, strategy=pos.strategy)
                return
            logger.warning(f"TP market exit attempt {attempt}/{max_attempts} failed for {pos.symbol}: {result.message}")
            if attempt < max_attempts:
                await asyncio.sleep(settings.bracket_retry_delay)

        logger.error(f"TP market exit FAILED after {max_attempts} attempts for {pos.symbol}: {result.message}")
        await notifier.notify_tp_exit_failed(pos.symbol, result.message, strategy=pos.strategy)

    async def time_exit_all(self) -> None:
        """Force-close MIS positions and cancel their pending bracket orders.

        Called by the TimeExitScheduler at the configured time (e.g., 15:00 IST).
        CNC positions (swing strategies) are excluded — they survive overnight.
        Uses strategy-level close/cancel APIs for reliability.
        """
        # Separate MIS (intraday) from CNC (swing) positions
        mis_positions = {k: v for k, v in self._positions.items() if v.product == "MIS"}
        cnc_positions = {k: v for k, v in self._positions.items() if v.product != "MIS"}

        if cnc_positions:
            logger.info(
                f"Time exit: keeping {len(cnc_positions)} CNC position(s) open: "
                + ", ".join(p.symbol for p in cnc_positions.values())
            )

        if not mis_positions:
            logger.info("Time exit: no MIS positions to close")
            capital = self._risk_engine._last_known_capital or 0.0
            await notifier.notify_day_summary(
                trades=self._day_trades,
                wins=self._day_wins,
                losses=self._day_losses,
                net_pnl=self._day_pnl,
                capital=capital,
            )
            self._day_trades = 0
            self._day_wins = 0
            self._day_losses = 0
            self._day_pnl = 0.0
            return

        # Collect unique strategies from MIS positions only
        strategies: Set[str] = {pos.strategy for pos in mis_positions.values()}

        for strategy in strategies:
            # Cancel all pending orders first (SL-M + TP LIMIT legs)
            logger.info(f"Time exit: cancelling all pending orders for strategy={strategy}")
            await cancel_all_orders(strategy)

            # Close all open positions
            logger.info(f"Time exit: closing all positions for strategy={strategy}")
            await close_all_positions(strategy)

        # Clear only MIS positions and update risk engine
        for key, pos in mis_positions.items():
            self._risk_engine.record_close(pnl=0.0, symbol=pos.symbol)
            await notifier.notify_time_exit(pos.symbol, strategy=pos.strategy)
            logger.info(f"Time exit: cleared tracker entry {key}")
            del self._positions[key]

        # Send day summary after MIS positions are closed
        capital = self._risk_engine._last_known_capital or 0.0
        await notifier.notify_day_summary(
            trades=self._day_trades,
            wins=self._day_wins,
            losses=self._day_losses,
            net_pnl=self._day_pnl,
            capital=capital,
        )
        # Reset day counters for next session
        self._day_trades = 0
        self._day_wins = 0
        self._day_losses = 0
        self._day_pnl = 0.0

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
