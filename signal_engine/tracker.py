"""Position tracker — polls OpenAlgo to detect closed positions and update risk counters."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Set

from loguru import logger

from signal_engine.api_client import (
    cancel_all_orders,
    cancel_order,
    close_all_positions,
    fetch_open_position,
    fetch_order_status,
    fetch_positionbook,
    fetch_realised_pnl,
)
from signal_engine.executor import build_exit_order, place_sl_order, send_order
from signal_engine.models import Direction, OrderStatus
from signal_engine.risk import RiskEngine
from signal_engine import notifier
from signal_engine.timeutils import IST



def _compute_r(total_pnl: float, qty: int, entry: float, sl: float) -> float | None:
    """Compute R-multiple: total_pnl divided by initial 1R risk for the position."""
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0 or qty == 0:
        return None
    return total_pnl / (qty * risk_per_share)


# Guard 2 verdicts for an unconfirmed entry fill.
_FILL_PROCEED = "proceed"      # order traded (or is proven filled) — book the close
_FILL_WAIT = "wait"            # status still unresolved — retry on the next poll
_FILL_ORPHANED = "orphaned"    # order never traded — slot released, no trade recorded


def _break_even_base(pos) -> float:
    """Break-even reference price: the actual fill when known, else the signal entry."""
    return pos.fill_price if pos.fill_price > 0 else pos.entry_price


def _profit_lock_price(pos, ltp: float, base_entry: float, lock_ratio: float) -> float:
    """Stop price for a stalled position — break-even, or a locked fraction of open profit."""
    in_profit = (pos.direction == Direction.LONG and ltp > base_entry) or \
                (pos.direction != Direction.LONG and ltp < base_entry)
    if lock_ratio <= 0 or not in_profit:
        return base_entry
    unrealized = abs(ltp - base_entry)
    if pos.direction == Direction.LONG:
        return base_entry + unrealized * lock_ratio
    return base_entry - unrealized * lock_ratio


def _index_positionbook(book) -> dict:
    """Index a positionbook response by symbol -> (quantity, ltp)."""
    book_data: dict = {}
    for entry in book:
        sym = entry.get("symbol", "")
        qty = int(entry.get("quantity", 0))
        ltp = float(entry.get("ltp", 0) or 0)
        book_data[sym] = (qty, ltp)
    return book_data


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
    exit_pending: bool = False  # True while an exit handler is actively processing this position
    fill_price: float = 0.0     # Actual broker fill price for the entry order (0 = unknown)
    original_quantity: int = 0  # Set by register() — qty at entry, unchanged through partial exits
    realized_pnl: float = 0.0   # P&L accumulated from partial exits (for W/L classification at full close)
    exit_types: List[str] = field(default_factory=list)  # labels appended at each exit leg
    entry_time: datetime = field(default_factory=lambda: datetime.now(IST))
    be_stop_applied: bool = False  # True after no-progress detection moved SL to break-even
    ever_seen_nonzero_qty: bool = False  # True once positionbook confirmed qty > 0 (fill proof)
    # Entry-criteria context from the signal (Signal.context). Carried so the partial-exit path
    # can place the runner's stop at the level that triggered the entry.
    context: dict = field(default_factory=dict)


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
        self._day_no_progress_exits: int = 0  # firings of no-progress gate today (chop signal)
        self._chop_tightener_logged: bool = False  # one-shot info log when tightener engages
        self._day_summary_sent: bool = False  # prevent duplicate summaries
        self._completed_trades: List[TradeRecord] = []  # one record per closed position
        # Per-(key, log-kind) throttle to suppress repeated debug lines on every 5s poll.
        # Value = last time we emitted that log line for the position/kind pair.
        self._last_debug_log: Dict[tuple[str, str], datetime] = {}

    def day_context_line(self, max_trades: int | None = None) -> str:
        """Compact per-day running context used in Telegram close notifications."""
        return notifier.format_day_context(
            day_trades=self._day_trades,
            day_wins=self._day_wins,
            day_losses=self._day_losses,
            day_pnl=self._day_pnl,
            max_trades=max_trades,
        )

    def projected_day_context(
        self, trade_pnl: float, pnl_delta: float, max_trades: int | None = None,
    ) -> str:
        """Day context as if a trade with this total_pnl/pnl_delta were already counted.

        Used by full-exit paths in main.py where tracker.record_exit is not called
        (close happens via risk_engine.record_close + unregister, and day counters
        are incremented later by check_positions on the next poll).
        """
        return notifier.format_day_context(
            day_trades=self._day_trades + 1,
            day_wins=self._day_wins + (1 if trade_pnl >= 0 else 0),
            day_losses=self._day_losses + (0 if trade_pnl >= 0 else 1),
            day_pnl=self._day_pnl + pnl_delta,
            max_trades=max_trades,
        )

    def _should_log_debug(self, key: str, kind: str, interval_sec: int = 60) -> bool:
        """Return True if enough time has elapsed since the last debug log for this key/kind."""
        now = datetime.now(IST)
        last = self._last_debug_log.get((key, kind))
        if last is None or (now - last).total_seconds() >= interval_sec:
            self._last_debug_log[(key, kind)] = now
            return True
        return False

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
        # Purge stale throttle entries for this key so a later re-entry starts fresh
        for kind in ("age", "poll_wait"):
            self._last_debug_log.pop((key, kind), None)
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

    async def book_close(
        self,
        pos,
        *,
        pnl_delta: float,
        exit_price: "float | None",
        exit_types: list,
        hold_minutes: int,
        max_trades: "int | None" = None,
        new_realised_pnl: "float | None" = None,
    ) -> TradeRecord:
        """Book a position that has closed for good, and return its trade record.

        Single home for what every full-close path shares: derive the trade's
        economics, file the trade record, advance the day counters, and send the
        close notification. Used by the signal-driven exit, the SL-HIT reconcile and
        the tracker's own close detection.

        The caller keeps what is specific to its path — cancelling broker orders,
        unregistering the position, releasing the risk slot, and deciding whether the
        day summary should now go out.

        Time exit does not use this: it notifies through notify_time_exit and
        attributes P&L across positions rather than per close.
        """
        base_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price
        total_pnl = pos.realized_pnl + pnl_delta
        record = TradeRecord(
            symbol=pos.symbol,
            direction=pos.direction.value,
            entry_price=base_price,
            exit_price=exit_price,
            original_qty=pos.original_quantity or pos.quantity,
            total_pnl=total_pnl,
            r_multiple=_compute_r(total_pnl, pos.original_quantity or pos.quantity, base_price, pos.sl),
            exit_types=exit_types,
        )
        self.add_trade_record(record)
        self.record_exit(
            pnl=pnl_delta, is_partial=False, total_pnl=total_pnl,
            new_realised_pnl=new_realised_pnl,
        )
        await notifier.notify_position_closed(
            pos.symbol, total_pnl, strategy=pos.strategy, exit_price=exit_price,
            direction=pos.direction.value, r_multiple=record.r_multiple,
            entry_price=base_price, hold_minutes=hold_minutes,
            exit_types=exit_types,
            day_context=self.day_context_line(max_trades),
        )
        return record

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

        book_data = _index_positionbook(book)
        closed_keys = []

        # Snapshot the position list so concurrent _handle_exit_locked unregisters
        # don't mutate the dict mid-iteration (RuntimeError on dict-size-change).
        for key, pos in list(self._positions.items()):
            with logger.contextualize(symbol=pos.symbol):
                if await self._check_one_position(key, pos, book_data, _settings):
                    closed_keys.append(key)

        for key in closed_keys:
            del self._positions[key]

        await self._maybe_send_day_summary(closed_keys, _settings)

        # No-progress check: move SL to entry for stuck positions
        # ab_test_disable short-circuits the entire check without changing thresholds —
        # used for clean A/B comparison with the feature off.
        if (
            _settings.no_progress_enabled
            and not _settings.no_progress_ab_test_disable
            and self._positions
        ):
            await self._check_no_progress(book_data)

    async def _check_one_position(self, key: str, pos, book_data: dict, _settings) -> bool:
        """Evaluate one tracked position. True if its tracker entry should be removed."""
        # Race guard: if a concurrent exit handler already removed/replaced this
        # position, skip — it's already been accounted for via record_close.
        if self._positions.get(key) is not pos:
            return False

        qty, _ltp = book_data.get(pos.symbol, (0, 0.0))
        if qty != 0:
            if not pos.ever_seen_nonzero_qty:
                pos.ever_seen_nonzero_qty = True
            return False

        age = datetime.now(IST) - pos.entry_time
        if self._position_too_young(key, age, _settings):
            return False

        verdict = await self._verify_unconfirmed_fill(key, pos, age, _settings)
        if verdict == _FILL_ORPHANED:
            return True
        if verdict == _FILL_WAIT:
            return False

        return await self._book_broker_close(key, pos, age, _settings)

    def _position_too_young(self, key: str, age: timedelta, _settings) -> bool:
        """Guard 1 — a freshly registered position showing qty=0 is usually positionbook lag.

        Not a genuine SL hit in the first few seconds.
        """
        if age >= timedelta(seconds=_settings.tracker_min_position_age_seconds):
            return False
        if self._should_log_debug(key, "age", interval_sec=30):
            logger.debug(
                f"check_positions: {key} age={age.total_seconds():.0f}s < "
                f"min={_settings.tracker_min_position_age_seconds}s — skipping"
            )
        return True

    async def _verify_unconfirmed_fill(self, key: str, pos, age: timedelta, _settings) -> str:
        """Guard 2 — verify the entry order actually traded before recording a close.

        A rejected order never appears in positionbook; without this check the tracker
        would invent a phantom closed trade at entry price with 0 PnL.

        Returns _FILL_PROCEED, _FILL_WAIT, or _FILL_ORPHANED.
        """
        if not (pos.fill_price == 0.0 and pos.entry_order_id):
            return _FILL_PROCEED

        order_status = await fetch_order_status(pos.entry_order_id, pos.strategy)
        status_lower = order_status.lower()

        if status_lower in ("rejected", "cancelled", "cancel"):
            logger.warning(
                f"check_positions: {key} order {pos.entry_order_id} "
                f"was {status_lower} — releasing slot without recording trade"
            )
            await self._release_orphan(key, pos, f"order {status_lower} by broker")
            return _FILL_ORPHANED

        if status_lower in ("complete", "filled"):
            # order traded, position now closed (e.g. instant SL) — normal close processing
            return _FILL_PROCEED

        if age < timedelta(minutes=_settings.tracker_guard2_timeout_minutes):
            # Pending, unknown, or API error — wait for next poll cycle.
            # Throttled: poll runs every 5s but we only need periodic
            # visibility into stuck positions (not every cycle).
            if self._should_log_debug(key, "poll_wait", interval_sec=60):
                logger.debug(
                    f"check_positions: {key} order status={order_status!r} "
                    f"positionbook not yet updated — waiting "
                    f"(age={age.total_seconds()/60:.0f}min)"
                )
            return _FILL_WAIT

        return await self._resolve_stuck_order(key, pos, age, order_status)

    async def _resolve_stuck_order(self, key: str, pos, age: timedelta, order_status: str) -> str:
        """Decide the fate of an order whose status never resolved within the timeout."""
        if pos.ever_seen_nonzero_qty:
            # Positionbook previously confirmed qty > 0, so the order DID fill.
            # The orderstatus API is unreliable (intermittent 500s / empty responses),
            # but the fill is proven. Use signal entry price as fill fallback so
            # Guard 3 (zero-PnL orphan check) doesn't misfire on break-even closes.
            logger.warning(
                f"check_positions: {key} order status={order_status!r} unresolved "
                f"after {age.total_seconds()/60:.0f}min but position was confirmed "
                "in positionbook — processing as real close (not orphan)"
            )
            pos.fill_price = pos.entry_price
            return _FILL_PROCEED

        # Never appeared in positionbook with qty > 0 — order never filled.
        logger.error(
            f"check_positions: {key} order status={order_status!r} still unresolved "
            f"after {age.total_seconds()/60:.0f}min and never seen in positionbook "
            "— treating as orphaned rejection, releasing slot"
        )
        await self._release_orphan(
            key, pos,
            f"order status={order_status!r} unresolved after {age.total_seconds()/60:.0f}min, "
            "never seen in positionbook",
        )
        return _FILL_ORPHANED

    async def _release_orphan(self, key: str, pos, reason: str) -> None:
        """Cancel any orphaned SL, release the risk slot, and alert — no trade recorded."""
        if pos.sl_order_id:
            await cancel_order(pos.sl_order_id, pos.strategy)
            logger.info(f"check_positions: cancelled orphaned SL {pos.sl_order_id} for {key}")
        self._risk_engine.record_rejection(symbol=pos.symbol)
        await notifier.notify_orphaned_position(
            pos.symbol, pos.strategy, pos.direction.value,
            pos.entry_order_id, reason,
        )

    async def _book_broker_close(self, key: str, pos, age: timedelta, _settings) -> bool:
        """Book a broker-side close. True if the tracker entry should be removed."""
        async with self._pnl_lock:
            # Re-check under the lock: a concurrent _handle_exit_locked path may have
            # already recorded this close (SL HIT reconcile, TP exit). If so, skip
            # to avoid double record_close / phantom zero-PnL trade record.
            if self._positions.get(key) is not pos:
                return False
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
            await self._release_orphan(key, pos, "zero PnL delta with unconfirmed fill")
            return True

        self._risk_engine.record_close(pnl_delta, symbol=pos.symbol)
        await self._record_closed_trade(key, pos, age, pnl_delta, _settings)
        return True

    async def _record_closed_trade(
        self, key: str, pos, age: timedelta, pnl_delta: float, _settings
    ) -> None:
        """Book a close the broker performed (SL-M fill) via the shared close path."""
        # Implied exit fill price from the PnL delta — the broker does not report one.
        base_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price
        if pos.quantity > 0:
            exit_price = base_price + (pnl_delta / pos.quantity) if pos.direction == Direction.LONG \
                else base_price - (pnl_delta / pos.quantity)
        else:
            exit_price = None

        record = await self.book_close(
            pos,
            pnl_delta=pnl_delta,
            exit_price=exit_price,
            exit_types=pos.exit_types[:] if pos.exit_types else ["SL"],
            hold_minutes=int(age.total_seconds() / 60),
            max_trades=_settings.max_trades_per_day,
        )

        exit_str = f"{exit_price:.2f}" if exit_price is not None else "N/A"
        logger.info(
            f"Position closed: {key}, entry={base_price:.2f}, exit={exit_str}, "
            f"pnl_delta={pnl_delta:,.2f} total_pnl={record.total_pnl:,.2f} r={record.r_multiple}"
        )

    async def _maybe_send_day_summary(self, closed_keys: list, _settings) -> None:
        """Send day summary only if all positions are now closed AND we are within
        30 min of time_exit (or past it).

        Ghost-closes mid-morning can empty _positions prematurely; deferring to the
        time-exit scheduler prevents a premature summary being sent with stale /
        incorrect data.
        """
        if not (closed_keys and not self._positions):
            return
        if not _settings.time_exit_enabled:
            await self.send_day_summary()
            return
        now = datetime.now(IST)
        time_exit_today = now.replace(
            hour=_settings.time_exit_hour,
            minute=_settings.time_exit_minute,
            second=0, microsecond=0,
        )
        if (time_exit_today - now).total_seconds() / 60 <= 30:
            await self.send_day_summary()

    async def _check_no_progress(self, book_data: dict) -> None:
        """Move SL to entry fill price (or market-exit) for positions that haven't progressed toward TP1.

        Runs on every poll cycle after check_positions closes any SL-triggered positions.
        Only acts once per position (be_stop_applied flag prevents re-triggering).

        Two gates run independently:
          - Early gate (optional): fires at early_check_after_minutes if progress < early_min_progress_pct.
            Catches catastrophic stalls and frees the slot earlier.
          - Main gate: fires at check_after_minutes if progress < min_progress_pct.
            The historically tuned threshold (90min / 20%).
        Whichever gate's age threshold has been crossed AND its progress threshold is breached fires first.

        progress = (ltp - base_entry) / (tp - base_entry)   # LONG
                 = (base_entry - ltp) / (base_entry - tp)   # SHORT
        Where base_entry = fill_price (when use_fill_price=true and available) else signal entry_price.
        """
        from signal_engine.config import settings as _settings

        now = datetime.now(IST)
        gates = self._no_progress_gates(_settings)

        for pos in list(self._positions.values()):
            if pos.be_stop_applied:
                continue

            fired = self._evaluate_no_progress(pos, now, gates, book_data, _settings)
            if fired is None:
                continue

            with logger.contextualize(symbol=pos.symbol):
                await self._apply_no_progress_action(pos, now, fired, _settings)

    def _no_progress_gates(self, _settings) -> "List[tuple]":
        """Build the ordered gate list: (age_threshold, progress_threshold, label).

        Chop tightener: if today already hit `trigger_count` no-progress firings, the
        early gate's age threshold is shortened. Main gate is intentionally untouched
        to preserve slow-developing winners.
        """
        chop_active = (
            _settings.no_progress_chop_tightener_enabled
            and self._day_no_progress_exits >= _settings.no_progress_chop_tightener_trigger_count
        )
        early_minutes = (
            _settings.no_progress_chop_tightener_early_check_after_minutes
            if chop_active
            else _settings.no_progress_early_check_after_minutes
        )
        if chop_active and not self._chop_tightener_logged:
            logger.info(
                f"Chop tightener engaged: {self._day_no_progress_exits} no-progress exits today "
                f">= {_settings.no_progress_chop_tightener_trigger_count} trigger; "
                f"early gate {_settings.no_progress_early_check_after_minutes}min -> {early_minutes}min"
            )
            self._chop_tightener_logged = True

        gates: List[tuple] = []
        if _settings.no_progress_loss_cut_enabled:
            gates.append((
                timedelta(minutes=_settings.no_progress_loss_cut_min_age_minutes),
                _settings.no_progress_loss_cut_progress_threshold,  # negative threshold
                "loss-cut",
            ))
        if _settings.no_progress_early_check_enabled:
            gates.append((
                timedelta(minutes=early_minutes),
                _settings.no_progress_early_min_progress_pct,
                "early",
            ))
        gates.append((
            timedelta(minutes=_settings.no_progress_check_after_minutes),
            _settings.no_progress_min_progress_pct,
            "main",
        ))
        return gates

    def _evaluate_no_progress(self, pos, now, gates, book_data: dict, _settings):
        """Return firing details for a stalled position, or None if no gate fires.

        Returns (age, ltp, progress, progress_base, fired_threshold, fired_label).
        """
        age = now - pos.entry_time
        # Skip if age hasn't crossed even the earliest configured gate.
        if not any(age >= g[0] for g in gates):
            return None

        # Need valid TP and entry to compute progress.
        if pos.tp <= 0 or pos.entry_price <= 0:
            return None
        # Choose progress base: fill_price (accurate) or signal entry (legacy).
        if _settings.no_progress_use_fill_price and pos.fill_price > 0:
            progress_base = pos.fill_price
        else:
            progress_base = pos.entry_price
        tp_distance = abs(pos.tp - progress_base)
        if tp_distance <= 0:
            return None

        _, ltp = book_data.get(pos.symbol, (0, 0.0))
        if ltp <= 0:
            return None

        if pos.direction == Direction.LONG:
            progress = (ltp - progress_base) / tp_distance
        else:
            progress = (progress_base - ltp) / tp_distance

        # Find the first gate whose age threshold is crossed AND whose progress
        # threshold is breached. Iteration order: early first, then main.
        for age_threshold, progress_threshold, label in gates:
            if age >= age_threshold and progress < progress_threshold:
                return age, ltp, progress, progress_base, progress_threshold, label

        return None  # No gate fired — making enough progress for current age band.

    async def _apply_no_progress_action(self, pos, now, fired, _settings) -> None:
        """Cancel the SL, then either market-exit or move the stop to break-even."""
        age, ltp, progress, progress_base, fired_threshold, fired_label = fired

        base_entry = _break_even_base(pos)
        be_price = _profit_lock_price(pos, ltp, base_entry, _settings.no_progress_profit_lock_ratio)
        use_market_exit = self._should_market_exit(now, age, progress, _settings)

        action_label = "market-exit" if use_market_exit else \
                       ("profit-lock" if be_price != base_entry else "break-even")
        logger.info(
            f"No-progress [{pos.symbol}] gate={fired_label}: age={age.total_seconds()/60:.0f}min "
            f"ltp={ltp:.2f} entry={progress_base:.2f} tp={pos.tp:.2f} "
            f"progress={progress:.1%} < {fired_threshold:.0%} "
            f"-> {action_label}"
        )
        # Chop signal: count this firing toward the daily tightener.
        # Counted once per position via be_stop_applied dedup below.
        self._day_no_progress_exits += 1

        # Cancel existing SL order first (required before any sell on Indian brokers)
        if pos.sl_order_id:
            cancelled = await cancel_order(pos.sl_order_id, pos.strategy)
            if not cancelled:
                logger.warning(f"No-progress: failed to cancel SL {pos.sl_order_id} for {pos.symbol}")
            pos.sl_order_id = ""

        pos.be_stop_applied = True  # prevent re-triggering regardless of path

        if use_market_exit:
            await self._no_progress_market_exit(pos, age, ltp, base_entry, progress)
        else:
            await self._no_progress_break_even(pos, age, ltp, base_entry, be_price, progress)

    @staticmethod
    def _should_market_exit(now, age, progress: float, _settings) -> bool:
        """True if the position cannot reach TP before time exit at its current pace.

        Rate-based: project minutes needed to reach TP at current pace vs minutes remaining.
        """
        if not _settings.time_exit_enabled:
            return False
        time_exit_today = now.replace(
            hour=_settings.time_exit_hour,
            minute=_settings.time_exit_minute,
            second=0, microsecond=0,
        )
        minutes_to_exit = max(0, (time_exit_today - now).total_seconds() / 60)
        age_min = age.total_seconds() / 60
        rate_per_min = progress / age_min if age_min > 0 else 0.0
        if rate_per_min <= 0:
            return True  # zero rate — never reaching TP
        minutes_needed = (1.0 - progress) / rate_per_min
        return minutes_needed > minutes_to_exit

    async def _no_progress_market_exit(self, pos, age, ltp, base_entry, progress) -> None:
        """Close a stalled position at market."""
        exit_order = build_exit_order(
            symbol=pos.symbol,
            exchange=pos.exchange,
            quantity=pos.quantity,
            product=pos.product,
            strategy_tag=pos.strategy,
            direction=pos.direction,
        )
        result = await send_order(exit_order)
        if result.status == OrderStatus.SUCCESS:
            logger.info(f"No-progress market exit placed for {pos.symbol}: id={result.order_id}")
            await notifier.notify_no_progress_exit(
                pos.symbol, ltp, base_entry, progress,
                strategy=pos.strategy, direction=pos.direction.value,
                age_minutes=int(age.total_seconds() / 60),
            )
        else:
            logger.error(f"No-progress market exit failed for {pos.symbol}: {result.message}")
            await notifier.notify_sl_failed(
                pos.symbol,
                f"No-progress market exit failed after {age.total_seconds()/60:.0f}min: {result.message}",
                strategy=pos.strategy,
            )

    async def _no_progress_break_even(self, pos, age, ltp, base_entry, be_price, progress) -> None:
        """Move the stop to break-even (or the profit-lock price) on a stalled position."""
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
            logger.info(f"Break-even SL placed for {pos.symbol}: {be_price:.2f} id={sl_result.order_id}")
            await notifier.notify_be_stop_applied(
                pos.symbol, be_price, ltp, progress,
                strategy=pos.strategy, direction=pos.direction.value,
                age_minutes=int(age.total_seconds() / 60),
                entry_price=base_entry,
                # NOTE: pos.sl was just set to be_price above, so this always evaluates
                # to None. Preserved as-is by the 2026-08-23 refactor (behaviour-
                # preserving); capturing the prior SL before the assignment would change
                # the notification payload. See PRD "known defects".
                original_sl=pos.sl if pos.sl != be_price else None,
            )
        else:
            logger.error(f"No-progress: break-even SL failed for {pos.symbol}: {sl_result.message}")
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
        from signal_engine.config import settings as _settings

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
            self._reset_day_counters()
            return

        await self._square_off_strategies({pos.strategy for pos in mis_positions.values()})
        await self._verify_positions_closed(mis_positions)
        await self._book_time_exit_trades(mis_positions, _settings)

        # Send day summary after MIS positions are closed (deduped — may have already fired)
        await self.send_day_summary()
        self._reset_day_counters()

    async def _square_off_strategies(self, strategies: Set[str]) -> None:
        """Cancel pending orders then close all positions, per strategy."""
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

    async def _verify_positions_closed(self, mis_positions: dict) -> None:
        """Confirm the broker actually closed everything; retry cancel+close if not."""
        _MAX_CLOSE_ATTEMPTS = 3
        _VERIFY_WAIT = 3  # seconds per attempt
        pending = list(mis_positions.items())  # [(key, pos), ...]

        for attempt in range(1, _MAX_CLOSE_ATTEMPTS + 1):
            await asyncio.sleep(_VERIFY_WAIT)
            still_open = []
            for k, pos in pending:
                qty = await fetch_open_position(
                    pos.symbol, pos.strategy, pos.exchange or "NSE", pos.product
                )
                if qty != 0 and qty != -1:
                    still_open.append((k, pos))

            if not still_open:
                logger.info(
                    f"Time exit: {len(mis_positions)} position(s) confirmed closed at broker "
                    f"(attempt {attempt})"
                )
                return

            pending = still_open
            logger.warning(
                f"Time exit: {len(still_open)} position(s) still open after attempt "
                f"{attempt}/{_MAX_CLOSE_ATTEMPTS}: "
                + ", ".join(pos.symbol for _, pos in still_open)
            )
            if attempt < _MAX_CLOSE_ATTEMPTS:
                for strategy in {pos.strategy for _, pos in still_open}:
                    await cancel_all_orders(strategy)
                    await close_all_positions(strategy)

        # All retries exhausted and positions still open
        symbols_str = ", ".join(pos.symbol for _, pos in pending)
        logger.error(
            f"Time exit: FAILED to close {symbols_str} after {_MAX_CLOSE_ATTEMPTS} attempts. "
            f"Broker auto-square-off will apply charges!"
        )
        await notifier.notify(
            f"TIME EXIT FAILED: {symbols_str} not closed after {_MAX_CLOSE_ATTEMPTS} attempts. "
            f"Close manually before 15:20!"
        )

    async def _book_time_exit_trades(self, mis_positions: dict, _settings) -> None:
        """Distribute the time-exit P&L across MIS positions and clear them."""
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
            with logger.contextualize(symbol=pos.symbol):
                await self._book_one_time_exit(key, pos, per_pos_pnl, _settings)

    async def _book_one_time_exit(self, key: str, pos, per_pos_pnl: float, _settings) -> None:
        """Record one force-closed MIS position and drop it from the tracker."""
        base_price = _break_even_base(pos)
        total_pnl = pos.realized_pnl + per_pos_pnl
        r = _compute_r(total_pnl, pos.original_quantity or pos.quantity, base_price, pos.sl)
        hold_min = int((datetime.now(IST) - pos.entry_time).total_seconds() / 60)
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
        self._day_trades += 1
        self._day_time_exits += 1
        await notifier.notify_time_exit(
            pos.symbol, strategy=pos.strategy, direction=pos.direction.value,
            pnl=total_pnl, r_multiple=r,
            entry_price=base_price, hold_minutes=hold_min,
            day_context=self.day_context_line(_settings.max_trades_per_day),
        )
        logger.info(f"Time exit: cleared tracker entry {key}")
        del self._positions[key]

    def _reset_day_counters(self) -> None:
        """Clear day summary state so the next session starts from zero."""
        self._day_trades = 0
        self._day_wins = 0
        self._day_losses = 0
        self._day_time_exits = 0
        self._day_pnl = 0.0
        self._day_no_progress_exits = 0
        self._chop_tightener_logged = False
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
        self._last_date = datetime.now(IST).date()

    async def start(self) -> None:
        self._running = True
        logger.info(f"Time exit scheduler started: {self._hour:02d}:{self._minute:02d} IST")

        while self._running:
            now = datetime.now(IST)

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
                # Distinguish: catch-up during market hours vs system woke from sleep after close
                past_market_close = now.hour > 15 or (now.hour == 15 and now.minute >= 30)
                if past_market_close:
                    logger.warning(
                        f"Time exit catch-up at {now.strftime('%H:%M')} IST: market already closed (15:30). "
                        f"Engine was likely suspended (system sleep). "
                        f"Broker auto-square-off should have closed open MIS positions at 15:20. "
                        f"Running tracker cleanup."
                    )
                else:
                    logger.info(f"Time exit: past configured time, firing catch-up exit")
                await self._tracker.time_exit_all()
                self._fired_today = True

            await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
        logger.info("Time exit scheduler stopped")
