"""Position tracker — polls OpenAlgo to detect closed positions and update risk counters."""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger

from signal_engine import db, notifier, order_reconcile
from signal_engine.api_client import (
    cancel_all_orders,
    cancel_order,
    close_all_positions,
    fetch_net_position,
    fetch_open_position,
    fetch_order_fill_price,
    fetch_order_status,
    fetch_orderbook,
    fetch_positionbook,
    fetch_realised_pnl,
    find_recent_complete_order_id,
)
from signal_engine.executor import build_exit_order, place_sl_order, send_order
from signal_engine.models import Direction, OrderStatus
from signal_engine.risk import RiskEngine
from signal_engine.timeutils import IST


def _dedupe_breakingtrade_overlap(records: list) -> list:
    """Drop BREAKINGTRADE-WATCHLIST records for any symbol BREAKINGTRADE also traded today.

    BREAKINGTRADE and BREAKINGTRADE-WATCHLIST collide by design (same scanner, same
    daily-changing universe — see config.yaml's duplicate_window_seconds comment), and the
    broker nets positions by (symbol, exchange, product) alone, with no strategy dimension
    (sandbox.position_manager.get_position_for_symbol takes no strategy argument). So when
    both fire on the same symbol the same day, it is really ONE broker position that two
    engine legs each computed an independent, unblended P&L for — not two positions. That
    produced the 2026-09-15 reconciliation mismatch (engine -1,573.43 vs broker -1,127.04).
    For reporting, BREAKINGTRADE is the confirmed signal and owns that symbol's P&L; the
    watchlist leg is dropped so it is not double-counted. A symbol only WATCHLIST traded
    (no BREAKINGTRADE entry that day) is untouched.

    This only changes what the day summary / per-strategy reports show — it does not change
    what orders get placed, and reconcile.py's engine-vs-broker canary deliberately keeps
    reading the full, undeduped trades.db so it still catches every real close.
    """
    confirmed_symbols = {r.symbol.upper() for r in records if r.strategy.upper() == "BREAKINGTRADE"}
    return [
        r
        for r in records
        if not (
            r.strategy.upper() == "BREAKINGTRADE-WATCHLIST"
            and r.symbol.upper() in confirmed_symbols
        )
    ]


def leg_pnl(direction, entry_fill: float, exit_fill: float, qty: int) -> float:
    """Gross P&L of one exit leg from the two confirmed fill prices."""
    move = exit_fill - entry_fill if direction == Direction.LONG else entry_fill - exit_fill
    return move * qty


def _compute_r(total_pnl: float, qty: int, entry: float, sl: float) -> float | None:
    """Compute R-multiple: total_pnl divided by initial 1R risk for the position."""
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0 or qty == 0:
        return None
    return total_pnl / (qty * risk_per_share)


# Guard 2 verdicts for an unconfirmed entry fill.
_FILL_PROCEED = "proceed"  # order traded (or is proven filled) — book the close
_FILL_WAIT = "wait"  # status still unresolved — retry on the next poll
_FILL_ORPHANED = "orphaned"  # order never traded — slot released, no trade recorded


def _break_even_base(pos) -> float:
    """Break-even reference price: the actual fill when known, else the signal entry."""
    return pos.fill_price if pos.fill_price > 0 else pos.entry_price


def _ladder_owns_exits(_settings, strategy: str) -> bool:
    """True when the runner (the shared Ladder or the strategy's trailing driver) owns the exits.

    The backtest never models no-progress, loss-cut or profit-lock, so a strategy whose exits
    are owned by the runner must not have the tracker add exits the backtest never scored.
    """
    from signal_engine.config import RUNNER_EXIT_OWNERS

    entry = _settings.runner.strategies.get(strategy.upper())
    return entry is not None and entry.exit_owner in RUNNER_EXIT_OWNERS


def _profit_lock_price(pos, ltp: float, base_entry: float, lock_ratio: float) -> float:
    """Stop price for a stalled position — break-even, or a locked fraction of open profit."""
    in_profit = (pos.direction == Direction.LONG and ltp > base_entry) or (
        pos.direction != Direction.LONG and ltp < base_entry
    )
    if lock_ratio <= 0 or not in_profit:
        return base_entry
    unrealized = abs(ltp - base_entry)
    if pos.direction == Direction.LONG:
        return base_entry + unrealized * lock_ratio
    return base_entry - unrealized * lock_ratio


@dataclass(frozen=True)
class BookEntry:
    """One positionbook row: quantity, last price, and the broker's own realised P&L.

    `realised` is None when the broker did not report a figure - deliberately not 0.0, since
    zero is both a real P&L and the orphan-detection signal, and "absent" must not be able
    to impersonate either.
    """

    quantity: int
    ltp: float
    realised: "float | None" = None


def _index_positionbook(book) -> dict:
    """Index a positionbook response by symbol -> BookEntry.

    The per-symbol realised P&L is carried because the alternative - a portfolio-level
    delta - misattributes every close that shares a poll cycle with another. See
    _book_broker_close().
    """
    book_data: dict = {}
    for entry in book:
        sym = entry.get("symbol", "")
        qty = int(entry.get("quantity", 0))
        ltp = float(entry.get("ltp", 0) or 0)
        raw = entry.get("today_realized_pnl", entry.get("pnl"))
        try:
            realised = None if raw is None else float(raw)
        except (TypeError, ValueError):
            realised = None
        book_data[sym] = BookEntry(qty, ltp, realised)
    return book_data


@dataclass
class TradeRecord:
    """Completed trade summary — one record per position lifecycle, appended to _completed_trades."""

    symbol: str
    direction: str  # "LONG" / "SHORT"
    strategy: str  # e.g. "ORB" — lets the EOD summary group/compare by strategy
    entry_price: float  # fill_price if available, else signal entry
    exit_price: float | None  # final exit price (None for time/force exits)
    original_qty: int
    total_pnl: float  # cumulative P&L across all exit legs (partial + final)
    r_multiple: float | None
    exit_types: list[str]  # e.g. ["TP1", "TP2"], ["SL"], ["TIME"], ["EXIT"]


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
    fill_price: float = 0.0  # Actual broker fill price for the entry order (0 = unknown)
    original_quantity: int = 0  # Set by register() — qty at entry, unchanged through partial exits
    realized_pnl: float = (
        0.0  # P&L accumulated from partial exits (for W/L classification at full close)
    )
    exit_types: list[str] = field(default_factory=list)  # labels appended at each exit leg
    entry_time: datetime = field(default_factory=lambda: datetime.now(IST))
    be_stop_applied: bool = False  # True after no-progress detection moved SL to break-even
    ever_seen_nonzero_qty: bool = False  # True once positionbook confirmed qty > 0 (fill proof)
    # Entry-criteria context from the signal (Signal.context). Carried so the partial-exit path
    # can place the runner's stop at the level that triggered the entry.
    context: dict = field(default_factory=dict)
    # SigID from the originating alert (PineScript strategies only — Python-sourced strategies
    # leave this ""). Carried onto every exit this position ever writes via save_tracker_exit(),
    # so the ledger's sig_id-keyed reconciliation can pair a tracker-detected close (broker
    # SL-M fill beating the TradingView alert here, no-progress exit, time exit) back to its
    # entry. Without it, that close writes sig_id=NULL and the ledger reports both the real
    # entry (falsely OPEN_AT_EOD/NO_EXIT_EVENT) and the close (falsely NO_ENTRY) as broken —
    # 2026-09-17: every BREAKOUT position that closed via tracker detection did exactly this.
    sig_id: str = ""
    # Poll-loop SL re-retry bookkeeping (see PositionTracker._retry_missing_sl). Only
    # relevant while sl_order_id == "" — a position whose initial bracket placement
    # exhausted all bracket.max_sl_retries attempts.
    sl_retry_count: int = 0
    last_sl_retry_at: datetime | None = None


#: Remembers which date's day summary has already gone out. A file rather than in-memory
#: state because the watchdog restarts the engine every 5 minutes until 15:25, and the
#: scheduler's catch-up branch fires time_exit_all() on any start after 14:45 — against
#: counters that are empty because the process just began. Without this, a 14:50 restart
#: reports "No trades taken today." over the real summary sent minutes earlier.
_DAY_SUMMARY_MARKER = os.path.join(os.path.dirname(__file__), "data", "day_summary")


def _read_marker_for_today() -> "str | None":
    """The marker's raw content if it matches today, else None.

    Any read problem also reads as None: a corrupt or unreadable marker must never be able
    to suppress the summary indefinitely. Sending twice is a nuisance; going silent for the
    rest of the day is the failure that matters.
    """
    try:
        with open(_DAY_SUMMARY_MARKER, encoding="utf-8") as fh:
            content = fh.read().strip()
    except OSError:
        return None
    if content.split("|", 1)[0] != datetime.now(IST).date().isoformat():
        return None
    return content


def _summary_already_sent_today() -> bool:
    """True if today's summary is already recorded as sent."""
    return _read_marker_for_today() is not None


def _prior_summary_trade_count() -> "int | None":
    """The trade count today's already-sent summary reported, or None if unsent or unknown
    (a bare-date marker from before this field existed).

    2026-09-17: a summary that went out prematurely (mid-morning, some trades still to come)
    permanently suppressed the genuine EOD one for the rest of the day, because
    _summary_already_sent_today() only ever answered "sent or not" — with no way to tell
    "sent, but incompletely" apart from "sent, and correct". Comparing this against the
    current trade count in send_day_summary() lets a later, more-complete call correct an
    earlier short one instead of silently no-op'ing for the rest of the day.
    """
    content = _read_marker_for_today()
    if content is None or "|" not in content:
        return None
    try:
        return int(content.split("|", 1)[1])
    except ValueError:
        return None


def _mark_summary_sent(trade_count: int) -> None:
    try:
        os.makedirs(os.path.dirname(_DAY_SUMMARY_MARKER), exist_ok=True)
        with open(_DAY_SUMMARY_MARKER, "w", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(IST).date().isoformat()}|{trade_count}")
    except OSError as e:
        logger.warning(f"Could not record day-summary marker: {e}")


#: Order statuses that mean the order can still execute (as executor._find_resting_sl_order).
_WORKING_ORDER_STATUSES = ("trigger pending", "open", "pending")


class PositionTracker:
    """Monitors open positions by polling OpenAlgo API.

    When a position closes (quantity drops to 0), it computes approximate P&L
    and updates the risk engine's loss counters.
    """

    def __init__(self, risk_engine: RiskEngine, poll_interval: int = 30):
        self._positions: dict[str, TrackedPosition] = {}
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
        # Date whose summary has been sent in THIS process. Paired with the on-disk
        # marker above, which covers restarts.
        self._day_summary_date = None
        self._completed_trades: list[TradeRecord] = []  # one record per closed position
        # Per-(key, log-kind) throttle to suppress repeated debug lines on every 5s poll.
        # Value = last time we emitted that log line for the position/kind pair.
        self._last_debug_log: dict[tuple[str, str], datetime] = {}
        #: Strategies whose unrealised loss was reported on the last poll — so one whose
        #: positions have all closed gets an explicit 0.0 rather than keeping a stale figure
        #: that would go on throttling it. See _push_unrealised().
        self._reported_unrealised: set = set()
        #: Consecutive failed positionbook polls, and whether the outage has been reported.
        #: See _POSITIONBOOK_OUTAGE_POLLS.
        self._positionbook_failures = 0
        self._positionbook_outage_alerted = False

    def day_context_line(self, max_trades: int | None = None) -> str:
        """Compact per-day running context, from trades.db - carried on EVERY close message.

        This read the in-memory counters, which __init__ sets to zero, so after a restart
        every close notification understated the day. The engine restarted twice on
        2026-09-11. Nothing user-facing is sourced from memory any more.
        """
        _records, counts = self._day_from_db()
        return notifier.format_day_context(
            day_trades=counts["trades"],
            day_wins=counts["wins"],
            day_losses=counts["losses"],
            day_pnl=counts["net_pnl"],
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

    def other_strategies_signed_qty(
        self, symbol: str, exchange: str, product: str, exclude_strategy: str
    ) -> int:
        """Signed quantity (long +, short -) other strategies hold on the same broker position.

        The broker nets by (symbol, exchange, product) with no strategy dimension, so a net
        figure includes every strategy that touched the symbol. This is the part of it that
        is not `exclude_strategy`'s, per this tracker's books.
        """
        total = 0
        for p in self._positions.values():
            if p.strategy == exclude_strategy:
                continue
            if (p.symbol, p.exchange, p.product) != (symbol, exchange, product):
                continue
            total += p.quantity if p.direction == Direction.LONG else -p.quantity
        return total

    def other_strategy_exit_in_flight(
        self, symbol: str, exchange: str, product: str, exclude_strategy: str
    ) -> bool:
        """True while another strategy is mid-exit on the same broker position.

        The broker nets by symbol, and an exit order can fill before the tracker unregisters the
        position. In that window net minus other-strategy quantity is wrong for everyone else
        (2026-10-13 HINDALCO: net 0, tracker still held +320, so the other tag's share read as
        -320 and a BUY 320 went out). Callers deriving a position from the net must wait.
        """
        return any(
            p.strategy != exclude_strategy
            and (p.symbol, p.exchange, p.product) == (symbol, exchange, product)
            and p.exit_pending
            for p in self._positions.values()
        )

    async def fetch_own_position(
        self, symbol: str, strategy: str, exchange: str, product: str
    ) -> "int | None":
        """Signed broker quantity attributable to `strategy`, or None if the broker is unreachable.

        Net broker quantity minus what other tracked strategies hold on the same symbol, so
        BREAKOUT and BREAKOUT-PY never mistake each other's shares for their own.
        """
        net = await fetch_net_position(symbol, strategy, exchange, product)
        if net is None:
            return None
        return net - self.other_strategies_signed_qty(symbol, exchange, product, strategy)

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
        # ... and the position's exit lock, for the same reason (see main._release_exit_lock).
        # Imported here rather than at module scope: main imports tracker, not the reverse.
        try:
            from signal_engine.main import _release_exit_lock

            _release_exit_lock(symbol, strategy)
        except Exception as e:  # noqa: BLE001 - bookkeeping must not break unregister
            logger.debug(f"Could not release exit lock for {key}: {e}")
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
            strategy=pos.strategy,
            entry_price=base_price,
            exit_price=exit_price,
            original_qty=pos.original_quantity or pos.quantity,
            total_pnl=total_pnl,
            r_multiple=_compute_r(
                total_pnl, pos.original_quantity or pos.quantity, base_price, pos.sl
            ),
            exit_types=exit_types,
        )
        self.add_trade_record(record)
        # The audit trail, not just the in-memory record. Without this the close reaches the
        # day counters and Telegram but never trades.db - which every performance report and
        # the ledger read - so the next restart's reconciliation books it a second time from
        # the broker's figures. See db.save_tracker_exit().
        #
        # quantity is the REMAINING size at close (pos.quantity, decremented by main.py on
        # every partial exit) - not original_quantity. order_id is the order that actually
        # closed this leg (pos.sl_order_id: the live stop, or the no-progress market exit,
        # which also stores its id there - see _no_progress_market_exit), left blank (falls
        # back to "-TRACKER-") when truly unknown. Reusing entry_order_id/original_quantity
        # here corrupted the audit trail for every multi-leg position: the ledger's
        # order_id-keyed reconciliation could never find the real closing broker fill, and
        # always saw a phantom "trades the engine never sent" of the true size (2026-09-21).
        self._save_close_row(
            pos,
            exit_price=exit_price,
            total_pnl=total_pnl,
            exit_types=exit_types,
            order_id=pos.sl_order_id,
        )
        self.record_exit(
            pnl=pnl_delta,
            is_partial=False,
            total_pnl=total_pnl,
            new_realised_pnl=new_realised_pnl,
        )
        # The close is already in the ledger and the counters. A failing notification must not
        # raise from here: the caller would treat the close as unbooked, release the position and
        # let the poll book the same close again (I2).
        try:
            await notifier.notify_position_closed(
                pos.symbol,
                total_pnl,
                strategy=pos.strategy,
                exit_price=exit_price,
                direction=pos.direction.value,
                r_multiple=record.r_multiple,
                entry_price=base_price,
                hold_minutes=hold_minutes,
                exit_types=exit_types,
                day_context=self.day_context_line(max_trades),
            )
        except Exception:
            logger.exception(f"Close notification failed for {pos.symbol}; the close stays booked")
        return record

    @staticmethod
    def _save_close_row(
        pos, *, exit_price, total_pnl: float, exit_types: list, order_id: str,
        extra_context: "dict | None" = None,
    ) -> None:
        """The one place a position's closing EXIT row is written (book_close, time exit).

        includes_leg_pnl records how much of total_pnl the position had already realised from
        partial legs when it closed, so db.fetch_day_trades() can tell a cumulative close from
        one that does not contain its legs. quantity is the REMAINING size at close.
        """
        db.save_tracker_exit(
            strategy=pos.strategy,
            symbol=pos.symbol,
            entry=pos.entry_price,
            sl=pos.sl,
            tp=pos.tp,
            quantity=pos.quantity,
            exit_price=exit_price,
            pnl=total_pnl,
            exit_types=exit_types,
            order_id=order_id,
            sig_id=pos.sig_id,
            includes_leg_pnl=pos.realized_pnl,
            extra_context=extra_context,
        )

    async def send_day_summary(self) -> None:
        """Send day summary to notify channel. No-op if already sent today with nothing new
        to report since.

        Called when the last open position closes (SL hit or TP exit), and again at
        time_exit if any positions remain. Deduped via _day_summary_sent flag.

        Unconditional — does not check whether positions remain or what time it is.
        Callers that fire this the moment a position book empties should go through
        maybe_send_day_summary() instead; this is the low-level primitive time_exit_all()
        itself uses once the day is genuinely over.

        Self-correcting: if a summary already went out today but trades.db now shows MORE
        completed trades than that summary reported, this sends a corrected one rather than
        staying silent for the rest of the day. 2026-09-17: a premature send (a handful of
        trades in, mid-morning) permanently blocked the real 14:45 EOD summary under the old
        pure "sent or not" guard — every caller of this method already applies its own gate
        on WHEN it's appropriate to call (maybe_send_day_summary's 30-minute-of-time-exit
        window, or time_exit_all() itself), so by the time execution reaches here a repeat
        call is exactly the case this correction exists for.
        """
        today = datetime.now(IST).date()
        records, counts, degraded = self._day_from_db(want_degraded=True)
        if self._day_summary_date == today or _summary_already_sent_today():
            prior_count = _prior_summary_trade_count()
            if prior_count is None or counts["trades"] <= prior_count:
                return
            logger.warning(
                f"Day summary: an earlier send today covered only {prior_count} trade(s); "
                f"{counts['trades']} are on record now - sending a corrected summary."
            )
        capital = self._risk_engine.total_last_known_capital() or 0.0
        # Per-strategy opening capital, for the per-strategy EOD summaries notifier.py sends
        # alongside the consolidated one — each strategy sizes off its OWN cached day-start
        # capital (see RiskEngine's per-strategy isolation), so the consolidated Capital: X -> Y
        # line is only ever meaningful per strategy, never pooled across them.
        strategy_capital = {
            r.strategy: self._risk_engine.last_known_capital_for(r.strategy) for r in records
        }
        sent = await notifier.notify_day_summary(
            trades=counts["trades"],
            wins=counts["wins"],
            losses=counts["losses"],
            net_pnl=counts["net_pnl"],
            capital=capital,
            time_exits=counts["time_exits"],
            trade_records=records,
            strategy_capital=strategy_capital,
            degraded=degraded,
        )
        if not sent:
            # 2026-09-09: this used to mark itself done unconditionally, so a Telegram
            # client that wasn't ready yet (transient - the client connects moments after
            # the listener starts) silently ate the day summary for good, with nothing to
            # retry against for the rest of the day. Leave both markers unset so the next
            # caller (poll cycle, restart) tries again.
            logger.warning("Day summary NOT delivered (notifier not ready) - will retry")
            return
        self._day_summary_date = today
        _mark_summary_sent(counts["trades"])

        # The canary, right after the day is reported: does trades.db agree with the broker?
        # Nothing compared the two before, which is how 2026-09-11 reported +986.41 on a real
        # +119.31 with full confidence. See signal_engine/reconcile.py.
        try:
            from signal_engine import reconcile

            await reconcile.check_and_alert()
        except Exception as e:  # noqa: BLE001 - the canary must never break the summary
            logger.warning(f"Day summary: reconciliation check failed: {e}")

    def _day_from_db(self, *, want_degraded: bool = False):
        """(trade_records, counts) for today, read from trades.db.

        The in-memory counters are only what happened since the engine last started, so a
        mid-day restart used to silently truncate the day - twice on 2026-09-11. The database
        is complete now that tracker-detected closes write EXIT rows (db.save_tracker_exit),
        so it is the source. Memory remains the fallback: a reporting failure should leave
        the summary stale, never empty.
        """
        try:
            rows = db.fetch_day_trades(self._trade_mode_for_db())
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Day summary: could not read trades.db ({e}) - falling back to this "
                "session's in-memory counters, which exclude anything before the last restart"
            )
            memory = (
                self._completed_trades,
                {
                    "trades": self._day_trades,
                    "wins": self._day_wins,
                    "losses": self._day_losses,
                    "net_pnl": self._day_pnl,
                    "time_exits": self._day_time_exits,
                    "execution_exits": 0,
                },
            )
            return (*memory, True) if want_degraded else memory

        records = [
            TradeRecord(
                symbol=r["symbol"],
                direction="LONG",
                strategy=r["strategy"],
                entry_price=r["entry"],
                exit_price=r["exit_price"],
                original_qty=r["quantity"],
                total_pnl=r["total_pnl"],
                r_multiple=_compute_r(r["total_pnl"], r["quantity"], r["entry"], r["sl"]),
                exit_types=r["exit_types"],
            )
            for r in rows
        ]
        records = _dedupe_breakingtrade_overlap(records)
        # Same classification the day counters use: a TIME exit is a trade but not a decided
        # win or loss - the clock closed it, not the strategy's own rule.
        # The same holds for a BAD_FILL_CLOSE: the execution guard closed it, which says
        # nothing about the entry rule. Both still count in trades and in net P&L, because
        # the money is real; only the win rate, a statement about the strategy, leaves them out.
        decided = [r for r in records if not (set(r.exit_types or []) & db.NOT_DECIDED_EXIT_TYPES)]
        counts = {
            "trades": len(records),
            "wins": sum(1 for r in decided if r.total_pnl >= 0),
            "losses": sum(1 for r in decided if r.total_pnl < 0),
            "net_pnl": sum(r.total_pnl for r in records),
            "time_exits": sum(1 for r in records if "TIME" in (r.exit_types or [])),
            "execution_exits": sum(
                1 for r in records if db.EXIT_BAD_FILL_CLOSE in (r.exit_types or [])
            ),
        }
        return (records, counts, False) if want_degraded else (records, counts)

    @staticmethod
    def _trade_mode_for_db() -> str:
        """The mode trades.db is stamping rows with right now - set once by startup."""
        return db._TRADE_MODE

    async def maybe_send_day_summary(self) -> None:
        """Send the day summary now, unless positions remain open or the day could
        plausibly still have trading left in it.

        "No open positions right now" is not the same as "done trading for the day":
        a genuine SL/TP exit can empty the book mid-morning while the entry window and
        max_trades_per_day still have room. send_day_summary() is one-shot (guarded by
        _day_summary_sent), so calling it the instant positions hit zero would
        permanently lock in an incomplete tally for the rest of the session — every
        later trade would still update the in-memory counters but never reach
        Telegram. Deferred to within 30 minutes of the scheduled time exit, or sent
        immediately if time_exit is disabled entirely (nothing to defer to).

        Every full-close path should call this rather than send_day_summary()
        directly: the signal-driven paths in main.py (SL-HIT reconcile, full TP exit,
        the invalid-partial-remainder conversion) and the tracker's own broker-close
        polling (_maybe_send_day_summary) all share this one gate.
        """
        if self._positions:
            return
        from signal_engine.config import settings as _settings

        if not _settings.time_exit_enabled:
            await self.send_day_summary()
            return
        now = datetime.now(IST)
        time_exit_today = now.replace(
            hour=_settings.time_exit_hour,
            minute=_settings.time_exit_minute,
            second=0,
            microsecond=0,
        )
        if (time_exit_today - now).total_seconds() / 60 <= 30:
            await self.send_day_summary()

    #: Consecutive failed positionbook polls before the outage is escalated. One failure is
    #: noise - the next cycle is tracking.poll_interval away. Six at 5s is ~30 seconds of the
    #: tracker being BLIND: no close detection, no no-progress gate, no time-exit trigger.
    #: On 2026-09-11 a 403 burst produced 16 in a row (11:50-11:52 IST) and the only trace
    #: was one WARNING per cycle, found afterwards in OpenAlgo's traffic log rather than the
    #: engine's own.
    _POSITIONBOOK_OUTAGE_POLLS = 6

    async def _note_positionbook_failure(self) -> None:
        """Count a failed poll and escalate once the tracker has been blind long enough."""
        self._positionbook_failures += 1
        logger.warning(
            f"check_positions: positionbook fetch failed "
            f"({self._positionbook_failures} in a row) - skipping this poll cycle"
        )
        if (
            self._positionbook_failures < self._POSITIONBOOK_OUTAGE_POLLS
            or self._positionbook_outage_alerted
        ):
            return
        message = (
            f"POSITION TRACKING BLIND\n{self._positionbook_failures} consecutive positionbook "
            f"failures. Close detection, no-progress gates and the time exit are all stalled "
            f"for {len(self._positions)} open position(s) until OpenAlgo answers again."
        )
        logger.critical(message.replace("\n", " "))
        try:
            delivered = await notifier.notify_event("positionbook_outage", message)
        except Exception as e:  # noqa: BLE001 - alerting must not break the poll loop
            logger.warning(f"Could not send positionbook outage alert: {e}")
            return
        if not delivered:
            # Same class of bug as 2026-09-09's day summary (see notifier.notify()): marking
            # this "alerted" on a silent no-op (notifier not ready, channel unconfigured)
            # would permanently suppress the one alert that exists to say tracking is blind -
            # leave the flag unset so the next failed poll retries the send.
            logger.warning(
                "Positionbook outage alert NOT delivered (notifier not ready) - will retry"
            )
            return
        self._positionbook_outage_alerted = True

    async def _note_positionbook_success(self) -> None:
        """Clear the outage counter, announcing recovery if one was reported."""
        if self._positionbook_outage_alerted:
            self._positionbook_outage_alerted = False
            failures = self._positionbook_failures
            logger.info(f"Positionbook recovered after {failures} failed polls")
            try:
                delivered = await notifier.notify_event(
                    "positionbook_recovered",
                    f"Position tracking RESUMED after {failures} failed polls.",
                )
                if not delivered:
                    logger.warning(
                        "Positionbook recovery alert NOT delivered (notifier not ready)"
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not send positionbook recovery alert: {e}")
        self._positionbook_failures = 0

    def _push_unrealised(self, book_data: dict) -> None:
        """Report each strategy's mark-to-market unrealised LOSS to the risk engine.

        check_exposure() has always added this to the day's realised loss before testing the
        daily limit — but nothing ever called update_unrealised(), so the term was
        permanently 0.0 and the "combined" gate was only ever the realised half. The poll
        loop is the natural place: it already has every open position and a fresh LTP for
        each from the same positionbook call.

        Only losses count. A position in profit reports 0.0 rather than a negative loss —
        the daily limit ADDS this figure to realised losses, so letting a winner credit
        against it would let one open runner mask a day of stop-outs. Every strategy with a
        tracked position is reported each cycle, and a strategy whose positions have all
        closed is explicitly zeroed, so a stale figure can never keep throttling it.
        """
        losses: dict[str, float] = dict.fromkeys(self._reported_unrealised, 0.0)
        for pos in self._positions.values():
            losses.setdefault(pos.strategy, 0.0)
            book_entry = book_data.get(pos.symbol)
            ltp = book_entry.ltp if book_entry else 0.0
            base = pos.fill_price or pos.entry_price
            if not ltp or not base:
                continue  # no usable mark - skip this leg rather than invent one
            move = (ltp - base) if pos.direction == Direction.LONG else (base - ltp)
            if move < 0:
                losses[pos.strategy] += abs(move) * abs(pos.quantity)

        for strategy, loss in losses.items():
            self._risk_engine.update_unrealised(loss, strategy)
        self._reported_unrealised = {
            s for s in losses if s in {p.strategy for p in self._positions.values()}
        }

    async def _check_mode_flip(self) -> None:
        """Notice a mid-session OpenAlgo mode change and alert on it.

        The poll loop is the only thing running continuously all session, so it is where
        this belongs. mode_guard does the comparing and the halting (and reports a flip
        exactly once); this only has to raise the alarm — see mode_guard's module docstring
        for what silently diverges when the mode changes underneath a running engine.
        """
        from signal_engine import mode_guard

        new_phase = await mode_guard.check_for_flip()
        if new_phase is None:
            return
        try:
            await notifier.notify_event(
                "mode_flip_halt", f"ENTRIES HALTED\n{mode_guard.halt_reason()}"
            )
        except Exception as e:
            logger.warning(f"Could not send mode-flip alert: {e}")

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

        # The square-off closes positions at the broker; if the poll saw them at qty 0 it would
        # book the same close the time exit is about to book (four real days: IOC 09-24,
        # HINDPETRO 09-29, NAUKRI 09-30, TCS 10-01), and with a ledger row per time exit that
        # is the same loss written, and taken on the risk counters, twice.
        if self._time_exit_active:
            return

        # Before the positionbook read, so an outage that fails it still re-checks (and keeps
        # alerting about) orders left unconfirmed; the sweep needs only the order book.
        try:
            from signal_engine.main import sweep_unresolved_orders

            await sweep_unresolved_orders()
        except Exception:  # noqa: BLE001 - the sweep must never break the poll
            logger.exception("Sweep of unresolved orders failed")

        book = await fetch_positionbook()
        if book is None:
            await self._note_positionbook_failure()
            return
        await self._note_positionbook_success()

        book_data = _index_positionbook(book)
        self._push_unrealised(book_data)
        await self._check_mode_flip()
        closed_keys = []

        # Snapshot the position list so concurrent _handle_exit_locked unregisters
        # don't mutate the dict mid-iteration (RuntimeError on dict-size-change).
        for key, pos in list(self._positions.items()):
            with logger.contextualize(symbol=pos.symbol):
                if await self._check_one_position(key, pos, book_data, _settings):
                    closed_keys.append(key)

        for key in closed_keys:
            self._positions.pop(key, None)  # another path may already have unregistered it

        await self._maybe_send_day_summary(closed_keys)
        await self._retry_missing_sl(book_data, _settings)

        # No-progress check: move SL to entry for stuck positions
        # ab_test_disable short-circuits the entire check without changing thresholds —
        # used for clean A/B comparison with the feature off.
        if (
            _settings.no_progress_enabled
            and not _settings.no_progress_ab_test_disable
            and self._positions
        ):
            await self._check_no_progress(book_data)

    async def _retry_missing_sl(self, book_data: dict, _settings) -> None:
        """Re-attempt SL placement for positions whose initial bracket exhausted.

        _place_entry_bracket's 5-attempt retry (executor.place_sl_order) runs its whole
        window in a few seconds — too fast to outlast a broker-side rate-limit window
        (e.g. Flattrade's per-minute cap). 2026-09-24: JIOFIN's second entry hit exactly
        that and was left with sl_order_id="" for the rest of the session, with nothing
        ever retrying it again. This piggybacks on the poll loop's existing 5s cadence to
        keep trying — cooled down per position so it doesn't hammer the API every cycle,
        and capped so a genuinely broken broker connection still surfaces as an alert
        instead of retrying forever.
        """
        if not _settings.bracket_enabled:
            return
        now = datetime.now(IST)
        for key, pos in list(self._positions.items()):
            if pos.sl_order_id or pos.exit_pending or pos.quantity <= 0:
                continue
            if order_reconcile.get_uncertain(pos.strategy, pos.symbol) is not None:
                continue  # an order for this position is unconfirmed: a stop now could reverse it
            entry = book_data.get(pos.symbol)
            if entry is None or entry.quantity == 0:
                continue  # not confirmed open yet, or already closed — nothing to protect
            if pos.sl_retry_count >= _settings.bracket_max_sl_reretries:
                continue  # already exhausted and alerted below
            if pos.last_sl_retry_at is not None:
                elapsed = (now - pos.last_sl_retry_at).total_seconds()
                if elapsed < _settings.bracket_sl_reretry_interval_seconds:
                    continue

            pos.sl_retry_count += 1
            pos.last_sl_retry_at = now
            with logger.contextualize(symbol=pos.symbol):
                logger.info(
                    f"Retrying missing SL for {key} "
                    f"(re-attempt {pos.sl_retry_count}/{_settings.bracket_max_sl_reretries})"
                )
                result = await place_sl_order(
                    symbol=pos.symbol,
                    exchange=pos.exchange,
                    direction=pos.direction,
                    quantity=pos.quantity,
                    sl_price=pos.sl,
                    product=pos.product,
                    strategy_tag=pos.strategy,
                )
                if result.status == OrderStatus.SUCCESS:
                    pos.sl_order_id = result.order_id
                    logger.info(f"SL re-placed for {key}: id={result.order_id}")
                    await notifier.notify_sl_placed(
                        pos.symbol,
                        result.order_id,
                        strategy=pos.strategy,
                        sl_price=pos.sl,
                    )
                elif pos.sl_retry_count >= _settings.bracket_max_sl_reretries:
                    logger.error(
                        f"SL re-retry exhausted for {key} after "
                        f"{pos.sl_retry_count} attempts — still unprotected"
                    )
                    await notifier.notify_sl_failed(
                        pos.symbol,
                        f"re-retry exhausted after {pos.sl_retry_count} attempts: {result.message}",
                        strategy=pos.strategy,
                    )

    async def _check_one_position(self, key: str, pos, book_data: dict, _settings) -> bool:
        """Evaluate one tracked position. True if its tracker entry should be removed."""
        # Race guard: if a concurrent exit handler already removed/replaced this
        # position, skip — it's already been accounted for via record_close.
        if self._positions.get(key) is not pos:
            return False

        entry = book_data.get(pos.symbol)
        qty = entry.quantity if entry else 0
        if qty != 0:
            if not pos.ever_seen_nonzero_qty:
                pos.ever_seen_nonzero_qty = True
            return False

        age = datetime.now(IST) - pos.entry_time
        if self._position_too_young(key, age, _settings):
            return False

        # One close, one booking (I2). An exit handler (a signal exit, an SL-hit reconcile or a
        # no-progress exit) that already owns this position will book its close, and its own
        # booking awaits leave the position registered at qty 0 for a moment. Booking it here too
        # wrote the same close twice (KOTAKBANK 2026-10-05: rows TP1 and SL, +1,915.95 twice, two
        # record_close calls). The check and the set are one synchronous step and come before the
        # fill verification, so no handler can slip in during its awaits. Marking the position
        # also makes a late signal exit stand down instead of selling a position that is flat.
        if pos.exit_pending:
            return False
        pos.exit_pending = True
        closed = False
        try:
            verdict = await self._verify_unconfirmed_fill(key, pos, age, _settings)
            if verdict == _FILL_ORPHANED:
                closed = True
            elif verdict != _FILL_WAIT:
                closed = await self._book_broker_close(key, pos, age, _settings, entry)
            return closed
        finally:
            if closed:
                # Remove it now, not after the whole poll loop: a time exit or a signal exit
                # starting in between must not find a booked position still registered.
                self._positions.pop(key, None)
            else:
                pos.exit_pending = False

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
                    f"(age={age.total_seconds() / 60:.0f}min)"
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
                f"after {age.total_seconds() / 60:.0f}min but position was confirmed "
                "in positionbook — processing as real close (not orphan)"
            )
            pos.fill_price = pos.entry_price
            return _FILL_PROCEED

        # Never appeared in positionbook with qty > 0 — order never filled.
        logger.error(
            f"check_positions: {key} order status={order_status!r} still unresolved "
            f"after {age.total_seconds() / 60:.0f}min and never seen in positionbook "
            "— treating as orphaned rejection, releasing slot"
        )
        await self._release_orphan(
            key,
            pos,
            f"order status={order_status!r} unresolved after {age.total_seconds() / 60:.0f}min, "
            "never seen in positionbook",
        )
        return _FILL_ORPHANED

    async def _release_orphan(self, key: str, pos, reason: str) -> None:
        """Cancel any orphaned SL, release the risk slot, flag the trade record, and alert.

        "No trade recorded" here means the tracker stops managing the position - but
        db.save() already wrote an entry row optimistically at order-placement time, and this
        is the exact moment the engine learns that row's outcome cannot be trusted (2026-09-09:
        this is precisely how HINDALCO's phantom fill happened - a broker outage left the order
        stuck, this path fired, and nothing marked the already-written row as suspect). Flag it
        so fetch_clean_trades() excludes it from analysis without deleting the record itself.
        """
        if pos.sl_order_id:
            await cancel_order(pos.sl_order_id, pos.strategy)
            logger.info(f"check_positions: cancelled orphaned SL {pos.sl_order_id} for {key}")
        self._risk_engine.record_rejection(strategy=pos.strategy, symbol=pos.symbol)
        if pos.entry_order_id:
            from signal_engine import db

            db.flag_data_quality(pos.entry_order_id, db.DATA_QUALITY_EXECUTION_ISSUE, reason)
        await notifier.notify_orphaned_position(
            pos.symbol,
            pos.strategy,
            pos.direction.value,
            pos.entry_order_id,
            reason,
        )

    async def _book_broker_close(
        self, key: str, pos, age: timedelta, _settings, book_entry=None
    ) -> bool:
        """Book a broker-side close. True if the tracker entry should be removed.

        P&L comes from the BROKER'S OWN per-symbol realised figure when the positionbook
        reports one. It used to come only from a portfolio-level delta
        (fetch_realised_pnl() - _last_realised_pnl), which is correct for exactly one close
        per poll cycle and wrong for every close that shares one: the first booked absorbed
        the whole delta and the rest got nothing. On 2026-09-11 ADANIENSOL was booked at
        +897.40 and ADANIENT at +0.00 in the same second, when the broker's own figure for
        ADANIENSOL was -197.20. The portfolio delta remains the fallback for brokers that
        report no per-symbol number.
        """
        async with self._pnl_lock:
            # Re-check under the lock: a concurrent _handle_exit_locked path may have
            # already recorded this close (SL HIT reconcile, TP exit). If so, skip
            # to avoid double record_close / phantom zero-PnL trade record.
            if self._positions.get(key) is not pos:
                return False
            current_realised = await fetch_realised_pnl()
            portfolio_delta = current_realised - self._last_realised_pnl
            self._last_realised_pnl = current_realised

        per_symbol = book_entry.realised if book_entry is not None else None
        ltp = book_entry.ltp if book_entry is not None else None
        # ANALYZE mode: the sandbox sets a fully-closed position's ltp to its own execution
        # price on both an order-driven close and an expiry settlement
        # (sandbox/execution_engine.py's "Position closed completely" branches;
        # sandbox/position_manager.py's settlement_price assignment), and MTM never overwrites
        # it afterward for a qty=0 position - so a price-derived delta is available and
        # reliable on every ordinary ANALYZE close, not only ones with prior partial-exit
        # history. (The `ltp and` guard below still protects against a future settlement
        # path that doesn't set it, should one ever be added.) This matters because the
        # sandbox's own "realised" bookkeeping
        # has its own confirmed bug independent of partial exits - BREAKOUT/TCS closed
        # 2026-09-25 with no partial-exit history and the sandbox's figure was -578.00 against
        # -821.60 true from the actual booked fills (see docs/strategy-daily-performance.md's
        # 2026-09-25 entry). LIVE mode's ltp is a real broker's last TRADED market price (see
        # broker/flattrade/mapping/order_data.py's "lp" mapping) - NOT a guarantee of the
        # closing order's own fill price - so live mode keeps trusting the broker's own
        # realised figure below, unchanged.
        sandbox_mode = self._trade_mode_for_db() == "analyze"
        base_fill = pos.fill_price if pos.fill_price > 0 else pos.entry_price
        # Best source: the stop order's OWN confirmed fill. The position book's ltp is per
        # symbol and the broker nets across strategies, so with two strategies on one symbol it
        # can be the other one's execution price.
        sl_fill = None
        if pos.sl_order_id and base_fill > 0:
            sl_fill = await fetch_order_fill_price(pos.sl_order_id, pos.strategy, max_attempts=1)
        if sl_fill is not None:
            pnl_delta = leg_pnl(pos.direction, base_fill, sl_fill, pos.quantity)
            logger.info(
                f"{key}: stop order {pos.sl_order_id} filled at {sl_fill:.2f} - "
                f"pnl_delta {pnl_delta:,.2f} from the order's own fill"
            )
        elif ltp and (pos.realized_pnl != 0.0 or sandbox_mode):
            # This position already has partial-exit P&L booked in pos.realized_pnl (main.py
            # tracks each partial leg's delta from its own confirmed fill price). The broker's
            # per-symbol "realised" figure is NOT reliably an incremental delta for just the
            # final leg on top of that - confirmed 2026-09-21: for BHARTIARTL it produced
            # 859.50 (correct, from the two partials) + 718.80 ("realised") = 1578.30 booked,
            # against 1382.40 true from the broker's own fills - and the broker's own
            # today_realized_pnl for that position independently landed on neither number
            # either, so it isn't safe to treat as a source of truth once a position has
            # partial-exit history. Compute the final leg's delta from price instead of
            # trusting the broker's stateful realised-pnl bookkeeping at all.
            base_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price
            pnl_delta = (
                (ltp - base_price) if pos.direction == Direction.LONG else (base_price - ltp)
            ) * pos.quantity
            logger.debug(
                f"{key}: deriving pnl_delta from price (ltp={ltp:.2f}) instead of the "
                f"broker/sandbox's realised figure ({per_symbol}) - "
                f"{'partial-exit history present' if pos.realized_pnl != 0.0 else 'analyze mode'}"
            )
        else:
            pnl_delta = portfolio_delta if per_symbol is None else per_symbol
            if sandbox_mode and not ltp:
                # The one case the ANALYZE-mode price-derivation above cannot cover: the
                # sandbox reported quantity=0 with no (or zero) ltp, so there is no execution
                # price to derive from - this falls back to the sandbox's own "realised"
                # figure, which is exactly the unreliable source the fix above exists to
                # avoid. Should not happen on an ordinary close (see the comment above this
                # branch) - logged at WARNING, not DEBUG, so it doesn't reproduce the
                # BREAKOUT/TCS incident silently if it ever does.
                logger.warning(
                    f"{key}: ANALYZE mode but the sandbox reported no ltp (closing execution "
                    f"price) - falling back to its realised figure ({per_symbol}), which is "
                    "not reliably correct (see the 2026-09-25 BREAKOUT/TCS incident this "
                    "branch normally avoids)"
                )
            elif per_symbol is None:
                logger.debug(
                    f"{key}: broker reported no per-symbol realised P&L - falling back to the "
                    f"portfolio delta ({portfolio_delta:,.2f})"
                )

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

        self._risk_engine.record_close(pnl_delta, strategy=pos.strategy, symbol=pos.symbol)
        await self._record_closed_trade(key, pos, age, pnl_delta, _settings)
        return True

    async def _record_closed_trade(
        self, key: str, pos, age: timedelta, pnl_delta: float, _settings
    ) -> None:
        """Book a close the broker performed (SL-M fill, or an out-of-band settlement) via the
        shared close path.

        Cancel any resting SL first. A normal SL-HIT close cancels the very order that just
        filled, which the broker safely no-ops. But an out-of-band close - e.g. OpenAlgo's own
        catch_up processor force-settling a stale MIS position - leaves the engine's SL-M order
        still resting live with nothing left to protect; nothing else in this path ever cancels
        it. Confirmed 2026-09-22: SBIN's SL-M order 26092255072374 sat live on the broker for
        ~4 hours after the engine's own close was booked here, until OpenAlgo's unrelated EOD
        squareoff finally cancelled it - and that delayed cancel's margin release then errored
        ("Refusing to release ... only 0.00 is reserved"), because the margin had already been
        freed by the earlier out-of-band settlement.
        """
        if pos.sl_order_id:
            try:
                cancelled = await cancel_order(pos.sl_order_id, pos.strategy)
            except Exception as e:  # noqa: BLE001 - a cancel failure must not block booking the close
                logger.error(
                    f"check_positions: cancel_order raised for SL {pos.sl_order_id} on {key}: {e}"
                )
                cancelled = False
            if cancelled:
                logger.info(f"check_positions: cancelled resting SL {pos.sl_order_id} for {key}")
            else:
                logger.debug(
                    f"check_positions: SL {pos.sl_order_id} for {key} was not cancellable "
                    "(already filled/closed, or already gone)"
                )
        # Implied exit fill price from the PnL delta — the broker does not report one.
        base_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price
        if pos.quantity > 0:
            exit_price = (
                base_price + (pnl_delta / pos.quantity)
                if pos.direction == Direction.LONG
                else base_price - (pnl_delta / pos.quantity)
            )
        else:
            exit_price = None

        record = await self.book_close(
            pos,
            pnl_delta=pnl_delta,
            exit_price=exit_price,
            # Only assume an SL hit when no other path has already claimed this exit. A
            # no-progress market exit sets pos.exit_types before the close is detected; all
            # four of 2026-09-11's closes were no-progress exits reported as "SL".
            exit_types=pos.exit_types[:] if pos.exit_types else ["SL"],
            hold_minutes=int(age.total_seconds() / 60),
            max_trades=_settings.max_trades_per_day,
        )

        exit_str = f"{exit_price:.2f}" if exit_price is not None else "N/A"
        logger.info(
            f"Position closed: {key}, entry={base_price:.2f}, exit={exit_str}, "
            f"pnl_delta={pnl_delta:,.2f} total_pnl={record.total_pnl:,.2f} r={record.r_multiple}"
        )

    async def _maybe_send_day_summary(self, closed_keys: list) -> None:
        """Send day summary only if this poll cycle closed something.

        Ghost-closes mid-morning can empty _positions prematurely; the time-of-day
        gate that prevents a premature summary lives in maybe_send_day_summary(),
        shared with the signal-driven close paths in main.py.
        """
        if not closed_keys:
            return
        await self.maybe_send_day_summary()

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
            if _ladder_owns_exits(_settings, pos.strategy):
                continue
            # The loop snapshot can outlive the position: a signal exit may own it, or may
            # already have booked and removed it, since an earlier iteration awaited.
            if pos.exit_pending or self._positions.get(self._key(pos.symbol, pos.strategy)) is not pos:
                continue

            fired = self._evaluate_no_progress(pos, now, gates, book_data, _settings)
            if fired is None:
                continue

            with logger.contextualize(symbol=pos.symbol):
                await self._apply_no_progress_action(pos, now, fired, _settings)

    def _no_progress_gates(self, _settings) -> "list[tuple]":
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

        gates: list[tuple] = []
        if _settings.no_progress_loss_cut_enabled:
            gates.append(
                (
                    timedelta(minutes=_settings.no_progress_loss_cut_min_age_minutes),
                    _settings.no_progress_loss_cut_progress_threshold,  # negative threshold
                    "loss-cut",
                )
            )
        if _settings.no_progress_early_check_enabled:
            gates.append(
                (
                    timedelta(minutes=early_minutes),
                    _settings.no_progress_early_min_progress_pct,
                    "early",
                )
            )
        gates.append(
            (
                timedelta(minutes=_settings.no_progress_check_after_minutes),
                _settings.no_progress_min_progress_pct,
                "main",
            )
        )
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

        np_entry = book_data.get(pos.symbol)
        ltp = np_entry.ltp if np_entry else 0.0
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

        # Claim the position before the first await (the caller checked exit_pending in the same
        # synchronous run), so a signal exit cannot place a second order on it while the stop is
        # cancelled and the market exit sent. Released below unless this action closed it.
        pos.exit_pending = True
        try:
            await self._no_progress_action_claimed(pos, now, age, ltp, progress, progress_base,
                                                   fired_threshold, fired_label, _settings)
        finally:
            if self._positions.get(self._key(pos.symbol, pos.strategy)) is pos:
                pos.exit_pending = False

    async def _no_progress_action_claimed(
        self, pos, now, age, ltp, progress, progress_base, fired_threshold, fired_label, _settings
    ) -> None:
        """The body of _apply_no_progress_action, run while the position is claimed."""
        base_entry = _break_even_base(pos)
        be_price = _profit_lock_price(pos, ltp, base_entry, _settings.no_progress_profit_lock_ratio)
        use_market_exit = self._should_market_exit(now, age, progress, _settings)

        action_label = (
            "market-exit"
            if use_market_exit
            else ("profit-lock" if be_price != base_entry else "break-even")
        )
        logger.info(
            f"No-progress [{pos.symbol}] gate={fired_label}: age={age.total_seconds() / 60:.0f}min "
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
                logger.warning(
                    f"No-progress: failed to cancel SL {pos.sl_order_id} for {pos.symbol}"
                )
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
            second=0,
            microsecond=0,
        )
        minutes_to_exit = max(0, (time_exit_today - now).total_seconds() / 60)
        age_min = age.total_seconds() / 60
        rate_per_min = progress / age_min if age_min > 0 else 0.0
        if rate_per_min <= 0:
            return True  # zero rate — never reaching TP
        minutes_needed = (1.0 - progress) / rate_per_min
        return minutes_needed > minutes_to_exit

    async def _no_progress_market_exit(self, pos, age, ltp, base_entry, progress) -> None:
        """Close a stalled position at market, booking the close from its own confirmed fill.

        Tag the position before sending anything: _record_closed_trade()'s deferred fallback
        below defaults an untagged close to ["SL"], and every no-progress market exit is not
        an SL hit (confirmed 2026-09-22: SBIN and three others that day were all reported as
        SL when none were).
        """
        pos.exit_types.append("NO-PROGRESS")
        exit_order = build_exit_order(
            symbol=pos.symbol,
            exchange=pos.exchange,
            quantity=pos.quantity,
            product=pos.product,
            strategy_tag=pos.strategy,
            direction=pos.direction,
        )
        result = await send_order(exit_order)
        if result.status != OrderStatus.SUCCESS:
            logger.error(f"No-progress market exit failed for {pos.symbol}: {result.message}")
            await notifier.notify_sl_failed(
                pos.symbol,
                f"No-progress market exit failed after {age.total_seconds() / 60:.0f}min: {result.message}",
                strategy=pos.strategy,
            )
            return

        logger.info(f"No-progress market exit placed for {pos.symbol}: id={result.order_id}")
        # book_close() reads sl_order_id as "the order that will close this position" -
        # the SL was just cancelled above, so this is the only closing order id we have.
        pos.sl_order_id = result.order_id
        await notifier.notify_no_progress_exit(
            pos.symbol,
            ltp,
            base_entry,
            progress,
            strategy=pos.strategy,
            direction=pos.direction.value,
            age_minutes=int(age.total_seconds() / 60),
        )

        # Book the close now, from this order's own confirmed fill - do not leave it for the
        # next check_positions() poll to discover qty=0 and derive P&L from the broker's
        # realised-P&L delta instead. Confirmed 2026-09-22: INFY's no-progress exit filled at
        # 1024.40 (a real -104.00 loss on entry 1021.80x40) but the deferred delta path booked
        # +281.00 instead, off a phantom implied exit price (1014.775) that was never traded -
        # the broker-delta is a portfolio-level snapshot that can be stale or shared with
        # another close in the same poll cycle, neither of which applies to an order this
        # function itself just placed and can confirm directly.
        fill_price = await fetch_order_fill_price(result.order_id, pos.strategy)
        if fill_price is None:
            logger.warning(
                f"No-progress exit for {pos.symbol}: could not confirm fill price for order "
                f"{result.order_id} - falling back to the next poll's broker-delta close"
            )
            return

        base_price = pos.fill_price if pos.fill_price > 0 else pos.entry_price
        pnl_delta = (
            (fill_price - base_price)
            if pos.direction == Direction.LONG
            else (base_price - fill_price)
        ) * pos.quantity

        async with self._pnl_lock:
            if self._positions.get(self._key(pos.symbol, pos.strategy)) is not pos:
                return  # a concurrent path already closed and unregistered this position
            current_realised = await fetch_realised_pnl()
            self._last_realised_pnl = current_realised

        from signal_engine.config import settings as _settings

        self._risk_engine.record_close(pnl_delta, strategy=pos.strategy, symbol=pos.symbol)
        await self.book_close(
            pos,
            pnl_delta=pnl_delta,
            exit_price=fill_price,
            exit_types=pos.exit_types[:],
            hold_minutes=int(age.total_seconds() / 60),
            max_trades=_settings.max_trades_per_day,
            new_realised_pnl=current_realised,
        )
        self.unregister(pos.symbol, pos.strategy)
        await self.maybe_send_day_summary()

    async def _no_progress_break_even(self, pos, age, ltp, base_entry, be_price, progress) -> None:
        """Move the stop to break-even (or the profit-lock price) on a stalled position."""
        original_sl = pos.sl  # captured before the assignment below overwrites it
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
            logger.info(
                f"Break-even SL placed for {pos.symbol}: {be_price:.2f} id={sl_result.order_id}"
            )
            await notifier.notify_be_stop_applied(
                pos.symbol,
                be_price,
                ltp,
                progress,
                strategy=pos.strategy,
                direction=pos.direction.value,
                age_minutes=int(age.total_seconds() / 60),
                entry_price=base_entry,
                original_sl=original_sl if original_sl != be_price else None,
            )
        else:
            logger.error(f"No-progress: break-even SL failed for {pos.symbol}: {sl_result.message}")
            await notifier.notify_sl_failed(
                pos.symbol,
                f"Break-even SL failed after {age.total_seconds() / 60:.0f}min no-progress: {sl_result.message}",
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

        # Whole seconds: broker order timestamps carry no sub-second part, so keeping one here
        # would make an order stamped in the same second look older than the square-off.
        started = datetime.now(IST).replace(microsecond=0)
        # Held from the first cancel to the last booking, so neither the poll nor a TP/EXIT
        # signal can book or act on a position the square-off is closing.
        self._time_exit_active = True
        try:
            uncancelled = await self._square_off_strategies(
                {pos.strategy for pos in mis_positions.values()}
            )
            still_open = await self._verify_positions_closed(mis_positions)
            await self._confirm_orders_cancelled(mis_positions, uncancelled)
            await self._book_time_exit_trades(mis_positions, _settings, started, still_open)
        finally:
            self._time_exit_active = False

        # Send day summary after MIS positions are closed (deduped — may have already fired)
        await self.send_day_summary()
        self._reset_day_counters()

    async def _square_off_strategies(self, strategies: set[str]) -> set[str]:
        """Cancel pending orders then close all positions, per strategy.

        Returns the strategies whose cancel could not be confirmed. The close is position-based
        (it reads each live net quantity), so it never over-closes; a stop left WORKING after the
        flatten could later open a reversed position, which _confirm_orders_cancelled prevents.
        """
        uncancelled: set[str] = set()
        for strategy in strategies:
            # Cancel all pending orders first (SL-M + TP LIMIT legs); one retry before the close
            logger.info(f"Time exit: cancelling all pending orders for strategy={strategy}")
            if not await cancel_all_orders(strategy):
                logger.warning(f"Time exit: cancel failed for strategy={strategy}, retrying once")
                if not await cancel_all_orders(strategy):
                    uncancelled.add(strategy)

            # Close all open positions
            logger.info(f"Time exit: closing all positions for strategy={strategy}")
            await close_all_positions(strategy)
        return uncancelled

    @staticmethod
    def _working_orders(book, strategies: set[str], symbols: set[str]) -> list[dict]:
        """Orders still working at the broker for the squared-off strategies.

        Matches the order's strategy tag; a row without one counts when it is an MIS order on a
        squared-off symbol (a stop we cannot attribute is treated as ours, the safe direction).
        """
        working = []
        for o in book or []:
            if not isinstance(o, dict):
                continue
            status = str(o.get("order_status") or o.get("status") or "").lower()
            if status not in _WORKING_ORDER_STATUSES:
                continue
            tag = str(o.get("strategy") or "").strip().upper()
            if tag:
                if tag in {s.upper() for s in strategies}:
                    working.append(o)
            elif str(o.get("product", "")).upper() == "MIS" and str(o.get("symbol", "")).upper() in {
                s.upper() for s in symbols
            }:
                working.append(o)
        return working

    async def _confirm_orders_cancelled(self, mis_positions: dict, uncancelled: set[str]) -> None:
        """After the flatten, confirm from the broker's ORDER BOOK that no order of the squared-off
        strategies is still working; a cancel API "success" is not taken as proof.

        A working order (or an unreadable order book) is re-cancelled up to _CANCEL_RETRIES times.
        If that cannot be confirmed, a critical log and a Telegram alert go out, whether or not the
        positions are flat: a stop left working on a flat position can later open a reversed one.
        Cancels only: this never places an order.
        """
        _CANCEL_RETRIES = 3
        _CANCEL_WAIT = 2  # seconds
        strategies = {pos.strategy for pos in mis_positions.values()}
        symbols = {pos.symbol for pos in mis_positions.values()}
        if uncancelled:
            logger.warning(f"Time exit: cancel not acknowledged for {', '.join(sorted(uncancelled))}")
        working: list[dict] = []
        book = None
        for attempt in range(_CANCEL_RETRIES + 1):
            book = await fetch_orderbook()
            working = self._working_orders(book, strategies, symbols)
            if book is not None and not working:
                if attempt or uncancelled:
                    logger.info("Time exit: no working orders left for the squared-off strategies")
                return
            if attempt == _CANCEL_RETRIES:
                break
            retry = {str(o.get("strategy") or "").strip() for o in working} & strategies or strategies
            logger.warning(
                "Time exit: "
                + ("order book unreadable" if book is None else f"{len(working)} order(s) still working")
                + f", re-cancelling ({attempt + 1}/{_CANCEL_RETRIES}) for {', '.join(sorted(retry))}"
            )
            if book is None:
                for strategy in retry:
                    await cancel_all_orders(strategy)
            else:
                # Cancel each working order by its id. The sandbox's cancelallorder filters status
                # "trigger_pending" but stores "trigger pending", so it skips every SL/SL-M while
                # reporting success (2026-10-12: PAYTM's stop then opened a long at 15:12).
                for o in working:
                    order_id = str(o.get("orderid") or o.get("order_id") or "")
                    tag = str(o.get("strategy") or "").strip() or next(iter(sorted(strategies)))
                    if order_id:
                        await cancel_order(order_id, tag)
                    else:
                        await cancel_all_orders(tag)
            await asyncio.sleep(_CANCEL_WAIT)

        if book is None:
            detail = "the order book could not be read to confirm the cancel"
        else:
            detail = "still working: " + ", ".join(
                f"{o.get('symbol')} {o.get('action')} {o.get('pricetype') or o.get('price_type') or ''} "
                f"id={o.get('orderid') or o.get('order_id')}".strip()
                for o in working
            )
        logger.critical(
            f"Time exit: pending orders for {', '.join(sorted(strategies))} not confirmed cancelled "
            f"({detail}); a stop order may still be working and could open a new position"
        )
        await notifier.notify(
            f"TIME EXIT: pending orders for {', '.join(sorted(strategies))} not confirmed cancelled "
            f"({detail}). A stop order may still be working and could open a new position - "
            "check the order book now."
        )

    async def _verify_positions_closed(self, mis_positions: dict) -> set:
        """Confirm the broker actually closed everything; retry cancel+close if not.

        Returns the keys of positions the broker still holds after the last attempt.
        """
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
                return set()

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
        return {k for k, _ in pending}

    async def _book_time_exit_trades(
        self, mis_positions: dict, _settings, started: datetime, still_open: set = frozenset()
    ) -> None:
        """Distribute the time-exit P&L across MIS positions and clear them."""
        current_realised = await fetch_realised_pnl()
        time_exit_pnl = current_realised - self._last_realised_pnl
        self._last_realised_pnl = current_realised
        if time_exit_pnl != 0:
            self._day_pnl += time_exit_pnl
            logger.info(f"Time exit PnL captured: {time_exit_pnl:,.2f}")

        # Attribute P&L per position from its own closing price (the position book's ltp for a
        # closed position is the execution price in ANALYZE mode). Splitting the portfolio
        # delta equally, as this used to, gave every position the average of all of them.
        # Positions with no usable price share what is left of the delta equally.
        priced = await self._time_exit_leg_pnls(mis_positions)
        unpriced = [k for k in mis_positions if k not in priced]
        leftover = time_exit_pnl - sum(
            priced[k] for k in priced
        )  # priced legs are only the closing leg; realised partials are added in booking
        shared = leftover / len(unpriced) if unpriced else 0.0

        book = await fetch_orderbook() or []
        used_order_ids: set[str] = set()

        # Clear only MIS positions and update risk engine
        for key, pos in mis_positions.items():
            with logger.contextualize(symbol=pos.symbol):
                if self._positions.get(key) is not pos or pos.exit_pending:
                    # Already booked, or being booked right now, by another path (the poll or a
                    # signal exit): booking it here too would write the same close twice.
                    logger.info(f"Time exit: {key} is booked or being booked elsewhere - skipping")
                    continue
                await self._book_one_time_exit(
                    key, pos, priced.get(key, shared), _settings,
                    book=book, started=started, confirmed_closed=key not in still_open,
                    used_order_ids=used_order_ids,
                )

    async def _time_exit_leg_pnls(self, mis_positions: dict) -> dict:
        """Closing-leg P&L per tracked key from the position book's closing prices."""
        book = await fetch_positionbook()
        if not book:
            return {}
        book_data = _index_positionbook(book)
        out = {}
        for key, pos in mis_positions.items():
            entry = book_data.get(pos.symbol)
            base = pos.fill_price if pos.fill_price > 0 else pos.entry_price
            if entry is None or entry.quantity != 0 or not entry.ltp or base <= 0:
                continue
            out[key] = leg_pnl(pos.direction, base, entry.ltp, pos.quantity)
        return out

    async def _time_exit_order(
        self, book: list, pos, started: datetime, used_order_ids: set
    ) -> tuple[str, "float | None"]:
        """The order that closed `pos` at the square-off and its fill, from the orderbook.

        The square-off goes through the strategy-level close API, which returns no per-position
        order id, so the closing order is found the way a timed-out exit is: the complete order
        on this symbol, opposite action and remaining quantity placed since the square-off began.
        """
        action = "SELL" if pos.direction == Direction.LONG else "BUY"
        order_id = find_recent_complete_order_id(
            book, pos.symbol, action, pos.quantity, started, frozenset(used_order_ids)
        )
        if not order_id:
            return "", None
        used_order_ids.add(order_id)
        return order_id, await fetch_order_fill_price(order_id, pos.strategy) or None

    async def _book_one_time_exit(
        self, key: str, pos, per_pos_pnl: float, _settings, *,
        book: list = (), started: "datetime | None" = None, confirmed_closed: bool = True,
        used_order_ids: "set | None" = None,
    ) -> None:
        """Record one force-closed MIS position and drop it from the tracker.

        The closing EXIT row goes through the same writer as every other full close, with the
        TIME exit type and the order lineage the orderbook gives. No row is written for a
        position the broker still holds: a row would claim a close that did not happen.
        """
        base_price = _break_even_base(pos)
        order_id, exit_fill = (
            await self._time_exit_order(
                list(book), pos, started, used_order_ids if used_order_ids is not None else set()
            )
            if confirmed_closed and started is not None else ("", None)
        )
        if exit_fill and base_price > 0:
            per_pos_pnl = leg_pnl(pos.direction, base_price, exit_fill, pos.quantity)
        total_pnl = pos.realized_pnl + per_pos_pnl
        exit_types = pos.exit_types[:] + ["TIME"]
        r = _compute_r(total_pnl, pos.original_quantity or pos.quantity, base_price, pos.sl)
        hold_min = int((datetime.now(IST) - pos.entry_time).total_seconds() / 60)
        self._completed_trades.append(
            TradeRecord(
                symbol=pos.symbol,
                direction=pos.direction.value,
                strategy=pos.strategy,
                entry_price=base_price,
                exit_price=exit_fill,
                original_qty=pos.original_quantity or pos.quantity,
                total_pnl=total_pnl,
                r_multiple=r,
                exit_types=exit_types,
            )
        )
        if confirmed_closed:
            self._save_close_row(
                pos,
                exit_price=exit_fill,
                total_pnl=total_pnl,
                exit_types=exit_types,
                order_id=order_id,
                extra_context={
                    "exit_class": "TIME_EXIT",
                    "entry_order_id": pos.entry_order_id,
                    "exit_order_id": order_id,
                    "exit_fill": exit_fill,
                },
            )
        else:
            logger.error(
                f"Time exit: {pos.symbol} is still open at the broker - no closing row written"
            )
        self._risk_engine.record_close(pnl=total_pnl, strategy=pos.strategy, symbol=pos.symbol)
        self._day_trades += 1
        self._day_time_exits += 1
        await notifier.notify_time_exit(
            pos.symbol,
            strategy=pos.strategy,
            direction=pos.direction.value,
            pnl=total_pnl,
            r_multiple=r,
            entry_price=base_price,
            hold_minutes=hold_min,
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
        # _day_summary_date is deliberately NOT cleared here. This runs immediately after
        # time_exit_all() has sent the summary, so clearing it would disarm the one-shot
        # guard for the rest of the day and let the next empty-book moment send a second,
        # zeroed summary contradicting the first. The date itself is what ends the day.
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
            if not self._fired_today and now.hour == self._hour and now.minute >= self._minute:
                logger.info(
                    f"Time exit triggered at {now.strftime('%H:%M')} IST "
                    f"(configured: {self._hour:02d}:{self._minute:02d})"
                )
                await self._tracker.time_exit_all()
                self._fired_today = True

            # Also fire if past the configured time (in case scheduler started late)
            if not self._fired_today and (
                now.hour > self._hour or (now.hour == self._hour and now.minute > self._minute + 5)
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
                    logger.info("Time exit: past configured time, firing catch-up exit")
                await self._tracker.time_exit_all()
                self._fired_today = True

            await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
        logger.info("Time exit scheduler stopped")
