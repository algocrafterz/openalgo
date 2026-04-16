"""Position tracker — polls OpenAlgo to detect closed positions and update risk counters."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Set

from loguru import logger

from signal_engine.api_client import (
    cancel_all_orders,
    cancel_order,
    close_all_positions,
    fetch_order_status,
    fetch_positionbook,
    fetch_realised_pnl,
)
from signal_engine.executor import place_sl_order
from signal_engine.models import Direction, OrderStatus
from signal_engine.risk import RiskEngine
from signal_engine import notifier

# IST offset from UTC
_IST = timezone(timedelta(hours=5, minutes=30))


def _compute_r(total_pnl: float, qty: int, entry: float, sl: float) -> float | None:
    """Compute R-multiple: total_pnl divided by initial 1R risk for the position."""
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0 or qty == 0:
        return None
    return total_pnl / (qty * risk_per_share)


@dataclass
class TradeRecord:
    """Completed trade summary — one record per position lifecycle, appended to _completed_trades."""
    symbol: str
    direction: str          # "LONG" / "SHORT"
    entry_price: float      # fill_price if available, else signal entry
    exit_price: float | None  # final exit price (None for time/force exits)
    original_qty: int
    total_pnl: float        # cumulative P&L across all exit legs (partial + final)
    r_multiple: float | None
    exit_types: List[str]   # e.g. ["TP1", "TP2"], ["SL"], ["TIME"], ["EXIT"]


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
    tp_order_id: str = ""  # unused: TP exit driven by TradingView TP HIT signal, not broker order
    exit_pending: bool = False  # True while an exit handler is actively processing this position
    fill_price: float = 0.0     # Actual broker fill price for the entry order (0 = unknown)
    original_quantity: int = 0  # Set by register() — qty at entry, unchanged through partial exits
    realized_pnl: float = 0.0   # P&L accumulated from partial exits (for W/L classification at full close)
    exit_types: List[str] = field(default_factory=list)  # labels appended at each exit leg
    entry_time: datetime = field(default_factory=lambda: datetime.now(_IST))
    be_stop_applied: bool = False  # True after no-progress detection moved SL to break-even


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
        self._pnl_lock: asyncio.Lock = asyncio.Lock()  # serialise _last_realised_pnl updates
        self._time_exit_active: bool = False  # True while time_exit_all() is in progress
        # Day summary counters (reset at midnight via TimeExitScheduler)
        self._day_trades: int = 0
        self._day_wins: int = 0
        self._day_losses: int = 0
        self._day_time_exits: int = 0  # positions force-closed at 15:00 (not W or L)
        self._day_pnl: float = 0.0
        self._day_summary_sent: bool = False  # prevent duplicate summaries
        self._completed_trades: List[TradeRecord] = []  # one record per closed position

    @property
    def tracked_count(self) -> int:
        return len(self._positions)

    @staticmethod
    def _key(symbol: str, strategy: str) -> str:
        """Normalise lookup key — upper-case both components to absorb case drift in signals."""
        return f"{symbol.upper()}:{strategy.upper()}"

    def register(self, position: TrackedPosition) -> None:
        """Register a new position to track. Sets original_quantity from quantity if not provided."""
        if position.original_quantity == 0:
            position.original_quantity = position.quantity
        key = self._key(position.symbol, position.strategy)
        self._positions[key] = position
        logger.info(f"Tracking position: {key} qty={position.quantity}")

    def find_position(self, symbol: str, strategy: str) -> "TrackedPosition | None":
        """Look up a tracked position by symbol and strategy. Returns None if not found."""
        return self._positions.get(self._key(symbol, strategy))

    def unregister(self, symbol: str, strategy: str) -> "TrackedPosition | None":
        """Remove and return a tracked position. Returns None if not found."""
        key = self._key(symbol, strategy)
        pos = self._positions.pop(key, None)
        if pos:
            logger.info(f"Unregistered position: {key}")
        return pos

    def record_exit(
        self,
        pnl: float,
        is_partial: bool = False,
        total_pnl: float | None = None,
        new_realised_pnl: float | None = None,
    ) -> None:
        """Record an exit event in day summary counters.

        Partial exits only accumulate P&L — they do NOT count as completed trades.
        A trade is counted once, at the final (full) exit.

        Args:
            pnl: Realised P&L delta for this specific exit leg.
            is_partial: True for partial exits (TP1 with runner remaining). Does not
                count as a completed trade — only updates _day_pnl.
            total_pnl: Cumulative P&L for the full trade (all legs). Used for W/L
                classification on full exit. If omitted, pnl is used instead.
            new_realised_pnl: Updated cumulative realised P&L from broker API snapshot.
        """
        self._day_pnl += pnl
        if not is_partial:
            effective_pnl = total_pnl if total_pnl is not None else pnl
            self._day_trades += 1
            if effective_pnl >= 0:
                self._day_wins += 1
            else:
                self._day_losses += 1
        if new_realised_pnl is not None:
            self._last_realised_pnl = new_realised_pnl
        logger.info(
            f"Exit recorded (partial={is_partial}): pnl={pnl:,.2f} total={total_pnl} "
            f"day_trades={self._day_trades} wins={self._day_wins} losses={self._day_losses} "
            f"day_pnl={self._day_pnl:,.2f}"
        )

    def add_trade_record(self, record: TradeRecord) -> None:
        """Append a completed trade record for EOD summary."""
        self._completed_trades.append(record)

    async def send_day_summary(self) -> None:
        """Send day summary to notify channel. No-op if already sent today or no trades.

        Called when the last open position closes (SL hit or TP exit), and again at
        time_exit if any positions remain. Deduped via _day_summary_sent flag.
        """
        if self._day_summary_sent:
            return
        capital = self._risk_engine._last_known_capital or 0.0
        await notifier.notify_day_summary(
            trades=self._day_trades,
            wins=self._day_wins,
            losses=self._day_losses,
            net_pnl=self._day_pnl,
            capital=capital,
            time_exits=self._day_time_exits,
            trade_records=self._completed_trades,
        )
        self._day_summary_sent = True

    async def check_positions(self) -> None:
        """Poll all tracked positions with a single positionbook call.

        Checks: if position qty == 0 (closed by broker SL-M or TP HIT signal), update risk counters.
        TP exit is driven by TradingView TP HIT signal -> signal engine _handle_exit pipeline.

        Three guards protect against phantom/ghost closes:
          Guard 1 — min age: skip positions younger than tracker_min_position_age_seconds.
          Guard 2 — order status: if fill was never confirmed, query broker order status before
                    recording a close. Rejected/cancelled orders release the slot via record_rejection.
          Guard 3 — orphan: zero PnL + unconfirmed fill = order never traded; treat as rejection.
        """
        from signal_engine.config import settings as _settings

        book = await fetch_positionbook()
        if book is None:
            logger.warning("check_positions: positionbook fetch failed — skipping this poll cycle")
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

            if qty != 0:
                continue

            # Guard 1: Min age — skip positions younger than the configured threshold.
            # A freshly registered position that shows qty=0 is almost certainly a
            # positionbook propagation lag, not a genuine SL hit in the first few seconds.
            age = datetime.now(_IST) - pos.entry_time
            if age < timedelta(seconds=_settings.tracker_min_position_age_seconds):
                logger.debug(
                    f"check_positions: {key} age={age.total_seconds():.0f}s < "
                    f"min={_settings.tracker_min_position_age_seconds}s — skipping"
                )
                continue

            # Guard 2: Order status — for positions whose fill was never confirmed,
            # verify the order actually traded before recording a close.
            # A rejected order never appears in positionbook; without this check the
            # tracker would invent a phantom closed trade at entry price with 0 PnL.
            if pos.fill_price == 0.0 and pos.entry_order_id:
                order_status = await fetch_order_status(pos.entry_order_id, pos.strategy)
                status_lower = order_status.lower()
                if status_lower in ("rejected", "cancelled", "cancel"):
                    logger.warning(
                        f"check_positions: {key} order {pos.entry_order_id} "
                        f"was {status_lower} — releasing slot without recording trade"
                    )
                    self._risk_engine.record_rejection(symbol=pos.symbol)
                    await notifier.notify_orphaned_position(
                        pos.symbol, pos.strategy, pos.direction.value,
                        pos.entry_order_id, f"order {status_lower} by broker",
                    )
                    closed_keys.append(key)
                    continue
                elif status_lower not in ("complete", "filled"):
                    # Pending, unknown, or API error — wait for next poll cycle
                    logger.debug(
                        f"check_positions: {key} order status={order_status!r} "
                        "positionbook not yet updated — waiting"
                    )
                    continue
                # status == complete/filled: order traded, position now closed (e.g. instant SL)
                # Fall through to normal close processing.

            # --- Position closed (qty dropped to 0) ---
            async with self._pnl_lock:
                current_realised = await fetch_realised_pnl()
                pnl_delta = current_realised - self._last_realised_pnl
                self._last_realised_pnl = current_realised

            # Guard 3: Orphan detection — zero PnL with unconfirmed fill.
            # This fires when Guard 2's order-status API call was unavailable (API error) but
            # the broker also shows 0 PnL, indicating the order never actually traded.
            if pnl_delta == 0.0 and pos.fill_price == 0.0 and pos.realized_pnl == 0.0:
                logger.warning(
                    f"check_positions: {key} — zero PnL delta with unconfirmed fill "
                    "(orphan position, likely broker rejection). Releasing slot."
                )
                self._risk_engine.record_rejection(symbol=pos.symbol)
                await notifier.notify_orphaned_position(
                    pos.symbol, pos.strategy, pos.direction.value,
                    pos.entry_order_id, "zero PnL delta with unconfirmed fill",
                )
                closed_keys.append(key)
                continue

            self._risk_engine.record_close(pnl_delta, symbol=pos.symbol)

            # Compute implied exit fill price from PnL delta
            # Use actual entry fill price if available, else fall back to signal entry
            base_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price
            if pos.quantity > 0:
                exit_price = base_price + (pnl_delta / pos.quantity) if pos.direction == Direction.LONG \
                    else base_price - (pnl_delta / pos.quantity)
            else:
                exit_price = None
            exit_str = f"{exit_price:.2f}" if exit_price is not None else "N/A"

            # Total trade P&L = partial exits already counted + this final close leg
            total_pnl = pos.realized_pnl + pnl_delta
            r = _compute_r(total_pnl, pos.original_quantity or pos.quantity, base_price, pos.sl)
            hold_min = int(age.total_seconds() / 60)

            logger.info(
                f"Position closed: {key}, entry={base_price:.2f}, exit={exit_str}, "
                f"pnl_delta={pnl_delta:,.2f} total_pnl={total_pnl:,.2f} r={r}"
            )

            self._completed_trades.append(TradeRecord(
                symbol=pos.symbol,
                direction=pos.direction.value,
                entry_price=base_price,
                exit_price=exit_price,
                original_qty=pos.original_quantity or pos.quantity,
                total_pnl=total_pnl,
                r_multiple=r,
                exit_types=pos.exit_types[:] if pos.exit_types else ["SL"],
            ))

            self._day_trades += 1
            self._day_pnl += pnl_delta
            if total_pnl >= 0:
                self._day_wins += 1
            else:
                self._day_losses += 1

            await notifier.notify_position_closed(
                pos.symbol, total_pnl, strategy=pos.strategy, exit_price=exit_price,
                direction=pos.direction.value, r_multiple=r,
                entry_price=base_price, hold_minutes=hold_min,
            )

            closed_keys.append(key)

        for key in closed_keys:
            del self._positions[key]

        # Send day summary if all positions are now closed
        if closed_keys and not self._positions:
            await self.send_day_summary()

        # No-progress check: move SL to entry for stuck positions
        if _settings.no_progress_enabled and self._positions:
            await self._check_no_progress(book_data)

    async def _check_no_progress(self, book_data: dict) -> None:
        """Move SL to entry fill price for positions that haven't progressed toward TP1.

        Runs on every poll cycle after check_positions closes any SL-triggered positions.
        Only acts once per position (be_stop_applied flag prevents re-triggering).

        Logic per position (after check_after_minutes have elapsed since entry):
          progress = (ltp - entry) / (tp1 - entry)   # LONG
                   = (entry - ltp) / (entry - tp1)   # SHORT
          If progress < min_progress_pct -> cancel existing SL, place new SL at fill price.
        """
        from signal_engine.config import settings as _settings

        now = datetime.now(_IST)
        min_age = timedelta(minutes=_settings.no_progress_check_after_minutes)

        for pos in list(self._positions.values()):
            if pos.be_stop_applied:
                continue

            age = now - pos.entry_time
            if age < min_age:
                continue

            # Need valid TP and entry to compute progress
            if pos.tp <= 0 or pos.entry_price <= 0:
                continue
            tp_distance = abs(pos.tp - pos.entry_price)
            if tp_distance <= 0:
                continue

            _, ltp = book_data.get(pos.symbol, (0, 0.0))
            if ltp <= 0:
                continue

            if pos.direction == Direction.LONG:
                progress = (ltp - pos.entry_price) / tp_distance
            else:
                progress = (pos.entry_price - ltp) / tp_distance

            if progress >= _settings.no_progress_min_progress_pct:
                continue  # Making progress — leave it

            # Use actual fill price as break-even; fall back to signal entry
            be_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price

            logger.info(
                f"No-progress [{pos.symbol}]: age={age.total_seconds()/60:.0f}min "
                f"ltp={ltp:.2f} entry={be_price:.2f} tp={pos.tp:.2f} "
                f"progress={progress:.1%} < {_settings.no_progress_min_progress_pct:.0%} "
                f"-> moving SL to break-even {be_price:.2f}"
            )

            # Cancel existing SL order before re-placing
            if pos.sl_order_id:
                cancelled = await cancel_order(pos.sl_order_id, pos.strategy)
                if not cancelled:
                    logger.warning(f"No-progress: failed to cancel SL {pos.sl_order_id} for {pos.symbol}")
                pos.sl_order_id = ""

            sl_result = await place_sl_order(
                symbol=pos.symbol,
                exchange=pos.exchange,
                direction=pos.direction,
                quantity=pos.quantity,
                sl_price=be_price,
                product=pos.product,
                strategy_tag=pos.strategy,
            )

            if sl_result.status == OrderStatus.SUCCESS:
                pos.sl = be_price
                pos.sl_order_id = sl_result.order_id
                pos.be_stop_applied = True
                logger.info(
                    f"Break-even SL placed for {pos.symbol}: {be_price:.2f} "
                    f"id={sl_result.order_id}"
                )
                await notifier.notify_be_stop_applied(
                    pos.symbol, be_price, ltp, progress,
                    strategy=pos.strategy, direction=pos.direction.value,
                    age_minutes=int(age.total_seconds() / 60),
                )
            else:
                logger.error(
                    f"No-progress: break-even SL placement failed for {pos.symbol}: {sl_result.message}"
                )
                await notifier.notify_sl_failed(
                    pos.symbol,
                    f"Break-even SL failed after {age.total_seconds()/60:.0f}min no-progress: {sl_result.message}",
                    strategy=pos.strategy,
                )

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
            await self.send_day_summary()
            self._day_trades = 0
            self._day_wins = 0
            self._day_losses = 0
            self._day_pnl = 0.0
            self._day_summary_sent = False
            self._completed_trades = []
            return

        # Collect unique strategies from MIS positions only
        strategies: Set[str] = {pos.strategy for pos in mis_positions.values()}

        self._time_exit_active = True
        try:
            for strategy in strategies:
                # Cancel all pending orders first (SL-M + TP LIMIT legs)
                logger.info(f"Time exit: cancelling all pending orders for strategy={strategy}")
                await cancel_all_orders(strategy)

                # Close all open positions
                logger.info(f"Time exit: closing all positions for strategy={strategy}")
                await close_all_positions(strategy)
        finally:
            self._time_exit_active = False

        # Capture actual PnL of time-exited positions from broker realised PnL delta.
        # close_all_positions is fire-and-forget so we wait briefly for fills.
        await asyncio.sleep(2)
        current_realised = await fetch_realised_pnl()
        time_exit_pnl = current_realised - self._last_realised_pnl
        self._last_realised_pnl = current_realised
        if time_exit_pnl != 0:
            self._day_pnl += time_exit_pnl
            logger.info(f"Time exit PnL captured: {time_exit_pnl:,.2f}")

        # Distribute time-exit P&L equally across MIS positions (exact for single position,
        # approximate for multiple — per-position broker fetch would require serial close flow)
        n = len(mis_positions)
        per_pos_pnl = time_exit_pnl / n if n > 0 else 0.0

        # Clear only MIS positions and update risk engine
        for key, pos in mis_positions.items():
            base_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price
            total_pnl = pos.realized_pnl + per_pos_pnl
            r = _compute_r(total_pnl, pos.original_quantity or pos.quantity, base_price, pos.sl)
            hold_min = int((datetime.now(_IST) - pos.entry_time).total_seconds() / 60)
            self._completed_trades.append(TradeRecord(
                symbol=pos.symbol,
                direction=pos.direction.value,
                entry_price=base_price,
                exit_price=None,
                original_qty=pos.original_quantity or pos.quantity,
                total_pnl=total_pnl,
                r_multiple=r,
                exit_types=pos.exit_types[:] + ["TIME"],
            ))
            self._risk_engine.record_close(pnl=0.0, symbol=pos.symbol)
            await notifier.notify_time_exit(
                pos.symbol, strategy=pos.strategy, direction=pos.direction.value,
                pnl=total_pnl, r_multiple=r,
                entry_price=base_price, hold_minutes=hold_min,
            )
            self._day_trades += 1
            self._day_time_exits += 1
            logger.info(f"Time exit: cleared tracker entry {key}")
            del self._positions[key]

        # Send day summary after MIS positions are closed (deduped — may have already fired)
        await self.send_day_summary()
        # Reset day counters for next session
        self._day_trades = 0
        self._day_wins = 0
        self._day_losses = 0
        self._day_time_exits = 0
        self._day_pnl = 0.0
        self._day_summary_sent = False
        self._completed_trades = []

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
