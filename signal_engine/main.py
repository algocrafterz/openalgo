"""Entry point and pipeline orchestration for the signal engine."""

import asyncio
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from signal_engine import freshness, notifier, order_reconcile, startup
from signal_engine.api_client import (
    MarginAPIError,
    cancel_order,
    fetch_available_capital,
    fetch_funds_available,
    fetch_margin,
    fetch_order_fill_price,
    fetch_order_status,
    fetch_orderbook,
    fetch_realised_pnl,
    fetch_trading_mode,
)
from signal_engine.config import settings
from signal_engine.db import (
    EXIT_ENTRY_REJECTED_SLIPPAGE,
    EXIT_ENTRY_REJECTED_STOP_BREACHED,
    EXIT_ENTRY_REJECTED_TP_OVERSHOT,
    fetch_entry_fill_directions,
    fetch_last_entry_trade,
    fetch_order_ids_today,
    save,
    save_declined,
    save_execution_exit,
)
from signal_engine.executor import (
    build_exit_order,
    build_order,
    place_sl_order,
    send_bracket_legs,
    send_order,
)
from signal_engine.logger_setup import setup_logger
from signal_engine.models import Direction, OrderStatus, Signal, TradeResult, ValidationStatus
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.pipeline import single_source
from signal_engine.risk_store import RISK_DB_PATH, RiskStore
from signal_engine.runtime import build_risk_engine
from signal_engine.strategy_core import trade_policy
from signal_engine.timeutils import IST
from signal_engine.tracker import PositionTracker, TrackedPosition, _compute_r, leg_pnl
from signal_engine.validator import validate

_OPENALGO_DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "db", "openalgo.db"))


def _is_be_series(symbol: str, exchange: str) -> bool:
    """Return True if the symbol is in the T2T (BE) series — MIS trading not allowed.

    Queries the symtoken table for the broker-side symbol name. A brsymbol ending
    in '-BE' (e.g. 'ZODIAC-BE') is in NSE's Trade-to-Trade segment and cannot be
    traded MIS at any Indian broker.

    Fails open on any DB error so a bad lookup never silently blocks a valid trade.
    """
    try:
        conn = sqlite3.connect(_OPENALGO_DB, timeout=3)
        row = conn.execute(
            "SELECT brsymbol FROM symtoken WHERE symbol = ? AND exchange = ? LIMIT 1",
            (symbol, exchange),
        ).fetchone()
        conn.close()
        if row:
            return str(row[0]).endswith("-BE")
    except Exception as e:
        logger.warning(f"BE series check failed for {symbol}: {e} — allowing trade")
    return False


# Persistent risk counter store (keyed by mode + date, survives restarts)
_risk_store = RiskStore(RISK_DB_PATH)

# Global risk engine instance (counters restored from store on startup)
risk_engine = build_risk_engine(_risk_store)

# Global position tracker
tracker = PositionTracker(risk_engine, poll_interval=settings.poll_interval)

# Per-position exit locks to serialize concurrent TP signal processing.
# TradingView fires all TP alerts that hit in the same bar simultaneously at bar close.
# Telethon dispatches each as a separate asyncio task, so without locking both handlers
# find the same open position and place duplicate exit/SL orders.
_exit_locks: dict[str, asyncio.Lock] = {}


def _exit_lock_key(symbol: str, strategy: str) -> str:
    return f"{symbol}:{strategy}"


def _get_exit_lock(symbol: str, strategy: str) -> asyncio.Lock:
    key = _exit_lock_key(symbol, strategy)
    if key not in _exit_locks:
        _exit_locks[key] = asyncio.Lock()
    return _exit_locks[key]


def _release_exit_lock(symbol: str, strategy: str) -> None:
    """Drop a closed position's lock.

    Nothing used to remove these, and the key space is "any NSE symbol" - BreakingTrade
    scans the whole F&O list, so a long session accumulates one Lock per name it ever
    touched. Small each, but unbounded is unbounded, and sessions now survive failures that
    used to end the process.

    A HELD lock is never dropped: another task is inside it, and removing the entry would let
    the next exit for that position take a fresh lock and run concurrently - exactly the
    duplicate-order race the lock exists to prevent. It is released on the next call instead.
    """
    key = _exit_lock_key(symbol, strategy)
    lock = _exit_locks.get(key)
    if lock is None or lock.locked():
        return
    del _exit_locks[key]


async def adjust_qty_for_margin(signal, raw_qty: int, live_capital: float) -> int:
    """Check whether risk-based qty fits within available broker margin.

    For NSE/BSE equity: SpanCalc doesn't support equity. Estimates margin as
    qty * entry * mis_margin_pct (20% for Flattrade). If the full-risk qty doesn't
    fit in live_capital, returns 0 (binary reject — never scales down). Scaled
    positions risk less than 1% and produce dwarf profits not worth commission cost.

    For derivatives (NFO etc.): queries SpanCalc API for exact margin. Scales down if
    actual margin > live_capital. Raises MarginAPIError on persistent API failure.

    Note: sizing uses day-start capital (equal risk weighting). This check uses live_capital
    as a safety floor only — it prevents broker rejections, not sizing adjustments.

    Returns raw_qty if margin check passes, or 0 if the trade cannot be taken at full size.
    """
    exchange = signal.exchange or settings.exchange
    if exchange in ("NSE", "BSE"):
        return _equity_margin_qty(signal, raw_qty, live_capital)
    return await _derivative_margin_qty(signal, raw_qty, live_capital)


def _equity_margin_qty(signal, raw_qty: int, live_capital: float) -> int:
    """Estimate equity MIS margin and reject outright if the full size does not fit.

    SpanCalc only supports derivatives, so equity margin is estimated. Binary reject:
    we never scale qty down — a scaled trade risks less than 1% and produces dwarf
    positions that are not worth the commission + slippage cost. Sizing uses day-start
    capital (equal risk weighting); this check uses live_capital as a hard floor to
    prevent broker rejections.
    """
    estimated_margin = raw_qty * signal.entry * settings.mis_margin_pct
    if estimated_margin <= live_capital:
        logger.debug(
            f"NSE equity margin check passed for {signal.symbol}: "
            f"est_margin={estimated_margin:,.0f} ({settings.mis_margin_pct:.0%} of {raw_qty}x{signal.entry:.2f}) "
            f"<= live_capital={live_capital:,.0f}"
        )
        return raw_qty
    logger.info(
        f"NSE equity margin floor: skipping {signal.symbol} — "
        f"est_margin={estimated_margin:,.0f} > live_capital={live_capital:,.0f}, "
        f"full qty {raw_qty} not feasible (won't scale down to preserve 1% risk)"
    )
    return 0


async def _derivative_margin_qty(signal, raw_qty: int, live_capital: float) -> int:
    """Query SpanCalc for the exact margin and scale qty down if it does not fit."""
    actual_margin = await fetch_margin(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        action="BUY" if signal.direction == Direction.LONG else "SELL",
        quantity=raw_qty,
        product=signal.product or settings.product,
    )

    if actual_margin <= live_capital:
        logger.debug(
            f"Margin check passed for {signal.symbol}: "
            f"margin={actual_margin:,.0f} <= capital={live_capital:,.0f}"
        )
        return raw_qty

    # Scale proportionally — MIS margin is linear with qty
    adjusted_qty = math.floor(raw_qty * live_capital / actual_margin)
    logger.info(
        f"Qty adjusted for {signal.symbol}: {raw_qty} -> {adjusted_qty} "
        f"(margin={actual_margin:,.0f} > capital={live_capital:,.0f})"
    )
    return adjusted_qty


def _resolve_exit_qty(signal, pos: TrackedPosition) -> tuple[int, bool]:
    """Determine exit quantity and whether this is a full exit.

    Priority order for exit fraction:
    1. signal.exit_qty_pct — from PineScript ExitQtyPct field in alert (source of truth)
    2. strategy_profiles tp_levels config — fallback for strategies without ExitQtyPct
    3. 1.0 (full exit) — default

    Returns (exit_qty, is_full_exit).
    """
    # 1. PineScript-provided ExitQtyPct takes priority
    _raw = getattr(signal, "exit_qty_pct", None)
    exit_pct = _raw if isinstance(_raw, (int, float)) else None

    # 2. Fall back to tp_levels config if PineScript didn't provide ExitQtyPct
    if exit_pct is None:
        tp_level = getattr(signal, "tp_level", None)
        profile = settings.strategy_profiles.get(signal.strategy.upper(), {})
        tp_levels = profile.get("tp_levels", {})
        if tp_level and tp_levels:
            exit_pct = tp_levels.get(tp_level.upper(), 1.0)

    # 3. Default: full exit
    if exit_pct is None:
        exit_pct = 1.0

    if exit_pct >= 1.0:
        return pos.quantity, True

    exit_qty = math.floor(pos.quantity * exit_pct)
    if exit_qty <= 0:
        exit_qty = 1  # Always exit at least 1 share
    if exit_qty >= pos.quantity:
        return pos.quantity, True

    return exit_qty, False


def compute_next_tp(pos: TrackedPosition, tp_level: str | None) -> tuple[str, float] | None:
    """Compute the next TP price after a partial exit.

    Uses pos.tp (TP1 price) and pos.entry_price to derive R-multiples:
    - TP1 hit → next is TP1.5 at entry + 1.5R
    - TP1.5 hit → next is TP2 at entry + 2.0R
    - TP2 hit → next is TP3 at entry + 3.0R (only reachable with extended runner tiers, where
      TP2 becomes a partial exit instead of always closing the position — see breakout.pine's
      useExtendedRunnerTiers)
    - TP3 or unknown → None (no further standard TP level)

    Returns (next_label, next_price) or None if the next level cannot be computed.
    """
    if not tp_level or pos.tp <= 0 or pos.entry_price <= 0:
        return None
    r_distance = abs(pos.tp - pos.entry_price)
    if r_distance <= 0:
        return None
    next_levels: dict[str, tuple[str, float]] = {
        "TP1": ("TP1.5", 1.5),
        "TP1.5": ("TP2", 2.0),
        "TP2": ("TP3", 3.0),
    }
    next_info = next_levels.get(tp_level.upper())
    if next_info is None:
        return None
    next_label, multiplier = next_info
    if pos.direction == Direction.LONG:
        next_price = pos.entry_price + multiplier * r_distance
    else:
        next_price = pos.entry_price - multiplier * r_distance
    return (next_label, next_price)


async def _handle_exit(signal) -> None:
    """Handle an EXIT signal — close (or partially close) an existing position.

    Supports multi-TP partial exits via strategy_profiles config:
    - TP1 with exit_pct=0.5 -> exit 50% qty, keep position tracked with reduced qty
    - TP2 with exit_pct=1.0 -> exit remaining qty, unregister position
    - No tp_level (safety EXIT) -> exit full qty, unregister position

    EXIT pipeline: look up tracker -> resolve qty -> cancel SL (if full exit) ->
    MARKET SELL -> update tracker -> record PnL -> save
    """
    await notifier.notify_exit_signal_received(signal.symbol, signal.strategy)

    # Serialize concurrent exit signals for the same position.
    # When multiple TP levels hit the same 5-min bar, TradingView fires all TP alerts
    # simultaneously at bar close. Telethon dispatches each as a separate asyncio task,
    # so without locking both handlers find the same position and place duplicate orders.
    # The lock ensures the second handler sees the already-closed/reduced position.
    async with _get_exit_lock(signal.symbol, signal.strategy):
        try:
            await _handle_exit_locked(signal)
        except BaseException:
            # exit_pending makes the tracker poll skip this position. A handler that dies while
            # holding it (an error, or a cancelled task) must not leave it owned, or a real
            # stop-hit close would never be booked and the risk slot never released.
            _release_exit_ownership(signal)
            raise


def _release_exit_ownership(signal) -> None:
    """Clear exit_pending on a position this handler failed to finish closing."""
    pos = tracker.find_position(signal.symbol, signal.strategy)
    if pos is not None:
        pos.exit_pending = False


async def _handle_exit_locked(signal) -> None:
    """Inner exit handler — must be called with the per-position exit lock held.

    Stages: abort guards -> position lookup/recovery -> SL-HIT reconcile ->
    resolve qty -> cancel SL -> send exit -> book P&L -> persist.
    """
    if _exit_blocked_by_time_exit(signal):
        return

    pos = tracker.find_position(signal.symbol, signal.strategy)
    if _exit_already_in_progress(signal, pos):
        return
    if pos is not None:
        pos.exit_pending = True

    if pos is None:
        pos = await _recover_position_from_broker(signal)
        if pos is None:
            return

    if await _abort_exit_on_rejected_entry(signal, pos):
        return

    tp_level = getattr(signal, "tp_level", None)
    if tp_level == "SL":
        await _reconcile_sl_hit(signal, pos)
        return

    exit_qty, is_full_exit = _resolve_exit_qty(signal, pos)
    logger.info(
        f"EXIT: {pos.symbol} tp_level={tp_level} exit_qty={exit_qty}/{pos.quantity} "
        f"full_exit={is_full_exit}"
    )

    sl_cancelled = await _cancel_sl_before_exit(pos)
    if tp_level == trade_policy.MISSED_STOP_SAFETY and not sl_cancelled:
        await _resolve_failed_missed_stop_cancel(signal, pos)
        return

    exit_order, trade_result = await _place_exit_order(pos, exit_qty)

    if trade_result.status == OrderStatus.SUCCESS:
        if not await _book_exit_result(signal, pos, tp_level, exit_qty, is_full_exit, trade_result):
            return
        if is_full_exit:
            # book_close() (via _finalize_full_exit -> tracker.book_close ->
            # db.save_tracker_exit) already wrote the authoritative EXIT row for this close,
            # with the real computed total_pnl/exit_types in its context. save() below writes
            # the RAW signal's own context, which is empty for a plain TP-HIT/EXIT alert - a
            # second EXIT row for the same close, at 0 recorded P&L. fetch_day_trades() reads
            # every EXIT row, so this silently double-counted the close as its own "trade"
            # (a phantom win, since 0 >= 0) everywhere trades.db is read: the ledger, the EOD
            # report, and the Telegram day summary. Partial exits still need this call - it
            # is the only row ever written for that leg (see db.fetch_day_trades()).
            return
    else:
        await _handle_exit_order_failure(pos, trade_result)

    save(signal, exit_order, trade_result)


def _exit_blocked_by_time_exit(signal) -> bool:
    """True if time_exit_all() owns the close and this exit must stand down.

    time_exit cancels all orders then fires close_all_positions — a concurrent TP signal
    would try to cancel the same SL (already gone) and place a duplicate exit order.
    """
    if not tracker._time_exit_active:
        return False
    logger.info(
        f"EXIT skipped for {signal.symbol}: time_exit_all() in progress, positions closing via time exit"
    )
    return True


def _exit_already_in_progress(signal, pos) -> bool:
    """True if another handler is mid-exit on this position.

    asyncio.Lock serializes tasks, but Telethon can dispatch multiple simultaneous TP
    alerts as separate tasks before any acquires the lock. The flag is set/cleared
    synchronously (no await between check and set), so it is safe in single-threaded
    asyncio — it catches the rare case where the lock re-enters before the previous
    handler fully clears the position.
    """
    if pos is not None and getattr(pos, "exit_pending", False) is True:
        logger.warning(
            f"EXIT: {signal.symbol} exit already in progress (duplicate TP signal), skipping"
        )
        return True
    return False


async def _recover_position_from_broker(signal) -> "TrackedPosition | None":
    """Rebuild a TrackedPosition after an engine restart lost tracker state.

    Queries the broker for the live quantity and the trades.db audit trail for the
    entry context. Returns None (after notifying) when there is nothing to exit.
    """
    logger.warning(
        f"EXIT: {signal.symbol} not in tracker for strategy={signal.strategy}, "
        "checking broker API (engine restart fallback)"
    )
    exchange = signal.exchange or settings.exchange
    product = signal.product or settings.product
    if tracker.other_strategy_exit_in_flight(signal.symbol, exchange, product, signal.strategy):
        # The attribution below is only valid when no other strategy's fill is half-booked.
        logger.warning(
            f"EXIT: {signal.symbol} for {signal.strategy} not recovered: another strategy is "
            "mid-exit on the same broker position, so its share cannot be derived yet; no order sent"
        )
        return None
    # The broker nets by symbol across strategies, so take only this strategy's share: a Pine
    # TP alert must not sell shares that belong to BREAKOUT-PY (TCS, 2026-10-01).
    api_qty = await tracker.fetch_own_position(signal.symbol, signal.strategy, exchange, product)
    if api_qty is None or api_qty == 0:
        logger.warning(f"EXIT: no open position for {signal.symbol} (strategy={signal.strategy})")
        await notifier.notify_exit_no_position(signal.symbol, signal.strategy)
        return None

    # Negative qty from broker means SHORT position; positive means LONG.
    # Use the absolute value for quantity and set direction accordingly.
    fallback_direction = Direction.SHORT if api_qty < 0 else Direction.LONG

    entry_price, sl_price, tp_price, entry_order_id = _recover_entry_context(signal)

    return TrackedPosition(
        symbol=signal.symbol,
        strategy=signal.strategy,
        exchange=exchange,
        product=product,
        entry_price=entry_price,
        quantity=abs(api_qty),
        sl=sl_price,
        tp=tp_price,
        direction=fallback_direction,
        entry_order_id=entry_order_id,
        sl_order_id="",
    )


def _recover_entry_context(signal) -> tuple[float, float, float, str]:
    """Recover (entry, sl, tp, order_id) for an exit signal from the audit trail.

    EXIT signals synthesize entry=sl=tp=0, so without this lookup the partial-exit
    SL re-placement logic computes new_sl=0 and skips the bracket, leaving the
    remaining qty un-protected. (RBLBANK incident, 2026-05-04: 18 qty unprotected
    after engine restart + TP1.) Falls back to the signal's own values.
    """
    recovered = fetch_last_entry_trade(signal.symbol, signal.strategy)
    if recovered is None:
        logger.warning(
            f"EXIT recovery [{signal.symbol}]: no entry trade found in trades.db today "
            "— partial-exit SL re-placement may be skipped"
        )
        return signal.entry, signal.sl, signal.tp, ""

    logger.info(
        f"EXIT recovery [{signal.symbol}]: restored from trades.db "
        f"entry={recovered['entry']} sl={recovered['sl']} tp={recovered['tp']} "
        f"order_id={recovered['order_id']}"
    )
    return recovered["entry"], recovered["sl"], recovered["tp"], recovered["order_id"]


async def _abort_exit_on_rejected_entry(signal, pos) -> bool:
    """Guard against phantom exits. True if the exit must be abandoned.

    If the entry order was never confirmed as filled, verify the broker status before
    placing an exit. A rejected entry leaves no real position; placing a SELL without
    one would create an unintended naked short.
    """
    if not (pos.entry_order_id and pos.fill_price == 0.0):
        return False

    order_status = await fetch_order_status(pos.entry_order_id, pos.strategy)
    status_lower = order_status.lower()
    if status_lower not in ("rejected", "cancelled", "cancel"):
        return False

    logger.warning(
        f"EXIT: {pos.symbol} entry order {pos.entry_order_id} was {status_lower} "
        "— aborting exit (no real position). Cancelling orphaned SL and releasing slot."
    )
    if pos.sl_order_id:
        await cancel_order(pos.sl_order_id, pos.strategy)
        logger.info(f"EXIT: cancelled orphaned SL {pos.sl_order_id} for {pos.symbol}")
    risk_engine.record_rejection(strategy=pos.strategy, symbol=pos.symbol)
    tracker.unregister(signal.symbol, signal.strategy)
    await notifier.notify_orphaned_position(
        pos.symbol,
        pos.strategy,
        pos.direction.value,
        pos.entry_order_id,
        f"exit blocked: entry order {status_lower}",
    )
    return True


async def _reconcile_sl_hit(signal, pos) -> None:
    """Book a close the broker SL-M already executed.

    Do NOT place another SELL (would create naked short). Just clean up tracker.
    """
    logger.info(
        f"EXIT: SL HIT reconcile for {pos.symbol} — no broker order placed, cleaning up tracker"
    )
    sl_fill = None
    if pos.sl_order_id:
        sl_fill = await fetch_order_fill_price(pos.sl_order_id, pos.strategy, max_attempts=1)
        await cancel_order(pos.sl_order_id, pos.strategy)
    pnl_delta, current_realised = await _book_realised_pnl_delta()
    base_price = pos.fill_price or pos.entry_price
    if sl_fill and base_price:
        # The stop's own fill, not the portfolio delta (which absorbs other strategies' closes).
        pnl_delta = leg_pnl(pos.direction, base_price, sl_fill, pos.quantity)
    await tracker.book_close(
        pos,
        pnl_delta=pnl_delta,
        exit_price=sl_fill or pos.sl,
        exit_types=["SL"],
        hold_minutes=_hold_minutes(pos),
        max_trades=settings.max_trades_per_day,
        new_realised_pnl=current_realised,
    )
    tracker.unregister(signal.symbol, signal.strategy)
    risk_engine.record_close(pnl=pnl_delta, strategy=pos.strategy, symbol=pos.symbol)
    await tracker.maybe_send_day_summary()


async def _cancel_sl_before_exit(pos) -> bool:
    """ALWAYS cancel SL before placing an exit order. True when no stop is left working.

    Indian brokers treat any SELL while SL SELL is active as a new SHORT position
    (FUND LIMIT INSUFFICIENT). SL must be cancelled first, even for partial exits.
    """
    if not pos.sl_order_id:
        return True
    success = await cancel_order(pos.sl_order_id, pos.strategy)
    if success:
        logger.info(f"EXIT: SL order {pos.sl_order_id} cancelled for {pos.symbol}")
    else:
        logger.warning(f"EXIT: failed to cancel SL {pos.sl_order_id} for {pos.symbol}")
    return bool(success)


async def _resolve_failed_missed_stop_cancel(signal, pos) -> None:
    """For a missed-stop safety exit whose SL cancel failed: never send a second close.

    The cancel can fail because the stop has just filled. Broker flat: book it as the stop hit.
    Otherwise (still held, or unknown) abort this exit; the runner retries on the next bar.
    Only the MISSED_STOP_SAFETY label takes this path, so every other exit is unchanged.
    """
    own = await tracker.fetch_own_position(
        pos.symbol, pos.strategy, pos.exchange or settings.exchange, pos.product or settings.product
    )
    if own == 0:
        logger.warning(f"EXIT: {pos.symbol} stop already executed at the broker; reconciling, no order sent")
        await _reconcile_sl_hit(signal, pos)
        return
    logger.warning(
        f"EXIT: {pos.symbol} missed-stop close aborted: SL cancel failed and broker position is "
        f"{own}; not sending a second close while the stop may be live (runner retries next bar)"
    )
    pos.exit_pending = False


async def _place_exit_order(pos, exit_qty: int):
    """Build and send the MARKET exit order. Returns (order, result)."""
    exit_order = build_exit_order(
        symbol=pos.symbol,
        exchange=pos.exchange,
        quantity=exit_qty,
        product=pos.product,
        strategy_tag=pos.strategy,
        direction=pos.direction,
    )
    return exit_order, await _send_exit_with_retries(exit_order, pos)


async def _send_exit_with_retries(exit_order, pos) -> TradeResult:
    """Send the MARKET exit order, retrying up to bracket_tp_exit_retries.

    MARKET orders rarely reject but network timeouts are possible. We retry on any
    non-SUCCESS status. After SL cancel the position is unprotected, so we must exit.

    A client-side TIMEOUT is never retried blindly: see order_reconcile for why. The order
    is resent only when the broker's order book was read and holds no copy of it. When that
    cannot be established, nothing more is sent for this position until a later attempt
    reconciles it (the held result is a TIMEOUT, so the caller leaves the position
    registered and the next exit signal or bar tries again).
    """
    held = await _settle_prior_uncertain_exit(exit_order, pos)
    if held is not None:
        return held
    trade_result = TradeResult(status=OrderStatus.ERROR, message="Not attempted")
    attempt_started = datetime.now(IST).replace(microsecond=0)  # broker stamps are whole seconds
    for _attempt in range(1, settings.bracket_tp_exit_retries + 1):
        trade_result = await send_order(exit_order)
        if trade_result.status == OrderStatus.SUCCESS:
            break
        logger.warning(
            f"EXIT attempt {_attempt}/{settings.bracket_tp_exit_retries} failed for "
            f"{pos.symbol}: {trade_result.message}"
        )
        if trade_result.status == OrderStatus.TIMEOUT:
            outcome = await _reconcile_exit(exit_order, pos, attempt_started)
            if outcome.status == OrderStatus.SUCCESS:
                logger.info(
                    f"EXIT for {pos.symbol} confirmed live on the broker despite client "
                    f"timeout: id={outcome.order_id} (attempt {_attempt})"
                )
                return outcome
            if outcome.status == OrderStatus.TIMEOUT:
                return outcome
        if _attempt < settings.bracket_tp_exit_retries:
            await asyncio.sleep(settings.bracket_retry_delay)
    return trade_result


async def _reconcile_exit(exit_order, pos, since: datetime) -> TradeResult:
    """Settle a timed-out exit against the order book.

    SUCCESS: the broker holds it, adopt the id. TIMEOUT: uncertain, the position is marked and
    a CRITICAL alert raised, do not resend. ERROR: the book was read and has no copy, a resend is
    safe.
    """
    result = await order_reconcile.reconcile_timed_out_order(
        symbol=exit_order.symbol,
        action=exit_order.action.value,
        quantity=exit_order.quantity,
        since=since,
        fetch_orderbook=fetch_orderbook,
        attempts=settings.entry_timeout_recovery_attempts,
        delay=settings.entry_timeout_recovery_delay_seconds,
    )
    if result.outcome == order_reconcile.FILLED:
        order_reconcile.clear_uncertain(pos.strategy, pos.symbol)
        if result.duplicate_ids:
            await _alert_duplicate_fills(exit_order, pos, result)
        return TradeResult(
            order_id=result.order_id, status=OrderStatus.SUCCESS, message="recovered after timeout"
        )
    if result.outcome == order_reconcile.ABSENT:
        order_reconcile.clear_uncertain(pos.strategy, pos.symbol)
        return TradeResult(status=OrderStatus.ERROR, message="timed out; no copy on the broker")
    order_reconcile.mark_uncertain(
        order_reconcile.UncertainOrder(
            strategy=pos.strategy, symbol=pos.symbol, action=exit_order.action.value,
            quantity=exit_order.quantity, since=since, kind="EXIT",
        )
    )
    return await _hold_uncertain_exit(exit_order, pos, result.detail)


async def _hold_uncertain_exit(exit_order, pos, detail: str) -> TradeResult:
    message = (
        f"UNCERTAIN: {exit_order.action.value} {exit_order.quantity} {pos.symbol} "
        f"[{pos.strategy}] timed out and could not be confirmed ({detail}); not resent"
    )
    logger.critical(f"EXIT {message}")
    await notifier.notify(
        f"ORDER UNCONFIRMED: {message}. No further orders go out for this position until the "
        "broker order book confirms it. Its stop may already be cancelled: check the broker now."
    )
    return TradeResult(status=OrderStatus.TIMEOUT, message=message)


async def _alert_duplicate_fills(exit_order, pos, result) -> None:
    ids = ", ".join(result.duplicate_ids)
    logger.critical(
        f"EXIT {pos.symbol} [{pos.strategy}]: the broker holds extra completed copies of "
        f"{exit_order.action.value} {exit_order.quantity} (adopted {result.order_id}, extra: {ids})"
    )
    await notifier.notify(
        f"DUPLICATE FILLS: {pos.symbol} [{pos.strategy}] {exit_order.action.value} "
        f"{exit_order.quantity} filled more than once (adopted {result.order_id}, extra: {ids}). "
        "The extras opened a position the engine does not track: check the broker now."
    )


async def _settle_prior_uncertain_exit(exit_order, pos) -> TradeResult | None:
    """Before any new exit order: resolve an earlier exit that was left unconfirmed.

    None means no hold applies and the exit may proceed. A SUCCESS result adopts the earlier
    order as this exit. A TIMEOUT result keeps the hold.
    """
    pending = order_reconcile.get_uncertain(pos.strategy, pos.symbol)
    if pending is None or pending.kind != "EXIT":
        return None
    result = await order_reconcile.reconcile_timed_out_order(
        symbol=pending.symbol,
        action=pending.action,
        quantity=pending.quantity,
        since=pending.since,
        fetch_orderbook=fetch_orderbook,
        attempts=settings.entry_timeout_recovery_attempts,
        delay=settings.entry_timeout_recovery_delay_seconds,
    )
    if result.outcome == order_reconcile.UNCERTAIN:
        return await _hold_uncertain_exit(exit_order, pos, result.detail)
    if result.outcome == order_reconcile.ABSENT:
        order_reconcile.clear_uncertain(pos.strategy, pos.symbol)
        logger.warning(f"EXIT {pos.symbol}: earlier unconfirmed exit is not on the broker; exiting afresh")
        return None
    if pending.quantity != exit_order.quantity:
        # The earlier order closed a different size than this exit wants: booking it as this
        # exit would misstate the position. Keep the hold for a person.
        return await _hold_uncertain_exit(
            exit_order, pos, f"earlier order {result.order_id} filled {pending.quantity}, not {exit_order.quantity}"
        )
    order_reconcile.clear_uncertain(pos.strategy, pos.symbol)
    if result.duplicate_ids:
        await _alert_duplicate_fills(exit_order, pos, result)
    return TradeResult(order_id=result.order_id, status=OrderStatus.SUCCESS, message="recovered after timeout")


async def _find_recent_market_exit_fill(order, since: datetime) -> TradeResult | None:
    """Look for a MARKET exit that actually filled on the broker despite a
    client-side TIMEOUT, before _send_exit_with_retries resends and double-books
    the exit (see the TIMEOUT branch above for why this matters).

    Matches on symbol + action + quantity among COMPLETE orders timestamped at or
    after `since` (when this exit attempt sequence started); see
    api_client.find_recent_complete_order_id for why recency is the only safe filter.
    """
    from signal_engine.api_client import fetch_orderbook, find_recent_complete_order_id

    book = await fetch_orderbook()
    if not book:
        return None
    order_id = find_recent_complete_order_id(
        book, order.symbol, order.action.value, order.quantity, since
    )
    if not order_id:
        return None
    return TradeResult(
        order_id=order_id, status=OrderStatus.SUCCESS, message="recovered after timeout"
    )


async def _book_realised_pnl_delta() -> tuple[float, float]:
    """Snapshot broker realised P&L and return (delta_since_last, new_total).

    The lock prevents a race with check_positions polling concurrently.
    """
    async with tracker._pnl_lock:
        current_realised = await fetch_realised_pnl()
        pnl_delta = current_realised - tracker._last_realised_pnl
        tracker._last_realised_pnl = current_realised
    return pnl_delta, current_realised


def _hold_minutes(pos) -> int:
    """Minutes the position has been open, or 0 if entry time is unknown."""
    if not hasattr(pos, "entry_time"):
        return 0
    return int((datetime.now(IST) - pos.entry_time).total_seconds() / 60)


async def _book_exit_result(
    signal, pos, tp_level, exit_qty: int, is_full_exit: bool, trade_result
) -> bool:
    """Book P&L for a filled exit order.

    Returns False when the exit has already been fully finalised and the caller must
    skip the DB save (the defensive invalid-remainder conversion), True otherwise.
    """
    logger.info(f"EXIT order placed for {pos.symbol}: id={trade_result.order_id} qty={exit_qty}")

    pnl_delta, current_realised = await _book_realised_pnl_delta()
    logger.info(
        f"EXIT PnL for {pos.symbol}: delta={pnl_delta:,.2f} (realised={current_realised:,.2f})"
    )

    base_price = pos.fill_price or pos.entry_price
    hold_min = _hold_minutes(pos)
    # The signal's TP is only the price the exit was ASKED for; a MARKET order fills at the
    # live price. Prefer the order's own confirmed fill for both the booked price and the P&L:
    # the portfolio-wide realised delta above also absorbs every other close that landed since
    # the last snapshot (PY RELIANCE 2026-10-01 was booked +1,871.0 against +1,362.1 true).
    approx_exit_price = signal.tp if signal.tp and signal.tp > 0 else None
    fill_price = await fetch_order_fill_price(trade_result.order_id, pos.strategy)
    if fill_price and base_price:
        approx_exit_price = fill_price
        pnl_delta = leg_pnl(pos.direction, base_price, fill_price, exit_qty)
        logger.info(
            f"EXIT fill for {pos.symbol}: {fill_price:.2f} -> leg pnl {pnl_delta:,.2f} "
            "(from the order's own fill, not the portfolio delta)"
        )
    else:
        logger.warning(
            f"EXIT fill price unavailable for {pos.symbol} order {trade_result.order_id}: "
            f"booking the broker realised delta {pnl_delta:,.2f} instead (may include other closes)"
        )

    if is_full_exit or pos.quantity - exit_qty <= 0:
        # book_close() files pos.sl_order_id as the order that closed the position. The stop was
        # cancelled by _cancel_sl_before_exit() but its id is still there, so without this the
        # EXIT row carries the dead stop's id and the ledger cannot match the real exit fill
        # (ETERNAL 2026-10-06). Same hand-off the no-progress exit does for its own order.
        pos.sl_order_id = trade_result.order_id

    if is_full_exit:
        # book_close advances the day counters and the realised-P&L snapshot.
        await _finalize_full_exit(
            signal,
            pos,
            tp_level,
            pnl_delta,
            hold_min,
            approx_exit_price,
            current_realised,
        )
        await tracker.maybe_send_day_summary()
        return True

    remaining = pos.quantity - exit_qty
    if remaining <= 0:
        await _finalize_invalid_partial(
            signal,
            pos,
            tp_level,
            exit_qty,
            pnl_delta,
            current_realised,
            hold_min,
            approx_exit_price,
        )
        return False

    await _finalize_partial_exit(
        pos,
        tp_level,
        exit_qty,
        remaining,
        pnl_delta,
        base_price,
        hold_min,
        override_sl=_ladder_sl_override(signal),
    )
    # Partial exit: accumulate P&L only — the trade is counted at its final close.
    tracker.record_exit(
        pnl=pnl_delta,
        is_partial=True,
        new_realised_pnl=current_realised,
    )
    # A partial leg's row is written by the plain save() below, not book_close() - it is
    # the ONLY persisted row for this leg (see the comment in _handle_exit_locked). Without
    # this, that row carries the raw TP-HIT alert's own context (empty) and result.fill_price
    # (never set for exit orders - only entries fetch it), so trades.db silently drops this
    # leg's pnl entirely. Confirmed 2026-09-22: INFY's 88-share partial leg (order
    # 26092281366297) landed with fill_price=NULL, context={} - the EOD ledger's own
    # engine-side P&L total then undercounted the round trip by this leg's pnl, which is
    # exactly the gap the day's reconciliation check flagged. Mirrors what book_close()
    # already attaches for a full exit (exit_price=approx_exit_price, pnl=total_pnl).
    trade_result.fill_price = approx_exit_price
    # "partial" tells db.fetch_day_trades() this row is one leg: the final close's pnl is
    # cumulative and already contains it, so the row must not be counted again.
    signal.context = {"pnl": pnl_delta, "exit_types": [tp_level or "TP"], "partial": True}
    return True


async def _finalize_full_exit(
    signal,
    pos,
    tp_level,
    pnl_delta: float,
    hold_min: int,
    approx_exit_price,
    current_realised: float,
) -> None:
    """Book the close, unregister the position, and free the risk slot."""
    await tracker.book_close(
        pos,
        pnl_delta=pnl_delta,
        exit_price=approx_exit_price,
        exit_types=pos.exit_types + [tp_level or "EXIT"],
        hold_minutes=hold_min,
        max_trades=settings.max_trades_per_day,
        new_realised_pnl=current_realised,
    )
    tracker.unregister(signal.symbol, signal.strategy)
    risk_engine.record_close(pnl=pnl_delta, strategy=pos.strategy, symbol=pos.symbol)
    # exit_pending does not need clearing — position is unregistered


async def _finalize_invalid_partial(
    signal,
    pos,
    tp_level,
    exit_qty: int,
    pnl_delta: float,
    current_realised: float,
    hold_min: int,
    approx_exit_price,
) -> None:
    """Defensive: a partial exit that leaves no shares is booked as a full close."""
    logger.warning(
        f"Partial exit produced invalid remainder {pos.quantity - exit_qty} for {pos.symbol} "
        f"(qty={pos.quantity}, exit_qty={exit_qty}), converting to full exit"
    )
    await tracker.book_close(
        pos,
        pnl_delta=pnl_delta,
        exit_price=approx_exit_price,
        exit_types=pos.exit_types + [tp_level or "EXIT"],
        hold_minutes=hold_min,
        max_trades=settings.max_trades_per_day,
        new_realised_pnl=current_realised,
    )
    tracker.unregister(signal.symbol, signal.strategy)
    risk_engine.record_close(pnl=pnl_delta, strategy=pos.strategy, symbol=pos.symbol)
    await tracker.maybe_send_day_summary()


async def _finalize_partial_exit(
    pos,
    tp_level,
    exit_qty: int,
    remaining: int,
    pnl_delta: float,
    base_price: float,
    hold_min: int,
    override_sl: float | None = None,
) -> None:
    """Reduce the tracked qty, re-protect the runner, and notify."""
    pos.quantity = remaining
    pos.sl_order_id = ""  # clear old SL id — will be updated below if re-placement succeeds
    logger.info(f"Partial exit: {pos.symbol} exited {exit_qty}, remaining {remaining}")

    await _replace_runner_sl(pos, remaining, tp_level, override_sl=override_sl)

    # R for this partial leg only (shows how far into the trade we are)
    r_partial = _compute_r(pnl_delta, exit_qty, base_price, pos.sl)
    next_tp_info = compute_next_tp(pos, tp_level)
    next_tp_label = next_tp_info[0] if next_tp_info else None
    next_tp_price = next_tp_info[1] if next_tp_info else None
    await notifier.notify_partial_exit(
        pos.symbol,
        exit_qty,
        remaining,
        tp_level or "",
        pnl_delta,
        strategy=pos.strategy,
        new_sl=pos.sl,
        next_tp_label=next_tp_label,
        next_tp_price=next_tp_price,
        direction=pos.direction.value,
        r_multiple=r_partial,
        entry_price=base_price,
        hold_minutes=hold_min,
    )
    # Accumulate partial P&L and record exit label for final TradeRecord
    pos.realized_pnl += pnl_delta
    pos.exit_types.append(tp_level or "TP")
    pos.exit_pending = False  # partial exit done — allow next TP signal


# Trigger tags are FAMILY-VARIANT, e.g. "VAH-RT", "PDH-BRK", "IBL-REJ". Only the family
# names a level, and the entry alert carries that level's price under one of these keys.
#
# The value-area families are why this is a MAP and not a tuple. breakout.pine emits the
# previous session's value area P-prefixed -- PVAH / PPOC / PVAL -- so its lines cannot be
# confused with TradingView's native Session Volume Profile, which plots the CURRENT day's
# VAH/POC/VAL. The trigger tag stayed bare ("VAH-RT"), so family and context key differ for
# exactly those three. Looking up context["vah"] silently missed every value-area runner and
# dropped it onto the TP1-buffer fallback.
_LEVEL_CONTEXT_KEYS = {
    "ORH": "orh",
    "ORM": "orm",
    "ORL": "orl",
    "IBH": "ibh",
    "IBM": "ibm",
    "IBL": "ibl",
    "VAH": "pvah",
    "POC": "ppoc",
    "VAL": "pval",
    "PDH": "pdh",
    "PDL": "pdl",
}


def structural_runner_sl(direction, entry_price: float, tp: float, context: dict):
    """Where the runner's stop belongs after a partial exit, or None to use the R-based rule.

    The trade's thesis is "price accepted beyond <level>". If price falls back through that
    level the thesis is dead, and that — not a fixed fraction of R — is the honest exit. The
    result is floored at entry so a position that has already banked half can never come back
    as a loser.

    Returns None for a plain ORB breakout (no key level behind it) or when the alert did not
    carry a usable price for the triggering family, leaving the caller on the TP1-buffer rule.
    """
    trigger = (context or {}).get("trigger", "")
    family = trigger.split("-")[0].upper() if trigger else ""
    key = _LEVEL_CONTEXT_KEYS.get(family)
    if key is None:
        return None
    # The bare family name is accepted as a fallback so an alert predating the P-prefix
    # rename still resolves. Never the other way round: the prefixed key is authoritative.
    raw = context.get(key, context.get(family.lower()))
    try:
        level = float(raw)
    except (TypeError, ValueError):
        return None
    if level <= 0:
        return None
    # Never worse than break-even, in either direction.
    return max(level, entry_price) if direction == Direction.LONG else min(level, entry_price)


_TP_LEVEL_R_MULTIPLIERS = {"TP1": 1.0, "TP1.5": 1.5, "TP2": 2.0, "TP3": 3.0}


def _tp_level_price(pos, tp_level: str) -> float | None:
    """Price of a named TP level, derived from TP1's R-distance. None if not computable."""
    multiplier = _TP_LEVEL_R_MULTIPLIERS.get((tp_level or "").upper())
    if multiplier is None or pos.tp <= 0 or pos.entry_price <= 0:
        return None
    r_distance = abs(pos.tp - pos.entry_price)
    if r_distance <= 0:
        return None
    if pos.direction == Direction.LONG:
        return pos.entry_price + multiplier * r_distance
    return pos.entry_price - multiplier * r_distance


def _ladder_sl_override(signal) -> float | None:
    """The Ladder's effective stop carried on a runner-generated EXIT, else None.

    Only a positive finite number in a dict context counts; anything else (Pine alerts, test
    doubles) falls through to the structural/ratchet rules.
    """
    ctx = getattr(signal, "context", None)
    if not isinstance(ctx, dict):
        return None
    value = ctx.get("ladder_sl")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


async def _replace_runner_sl(
    pos, remaining: int, tp_level: str | None = None, override_sl: float | None = None
) -> None:
    """Re-place the runner's SL after a partial TP exit.

    With `override_sl` (the runner Ladder's effective stop) that price is used instead of the
    rules below, so a runner-managed position has exactly one stop rule. It still only ever
    tightens, and goes through the same placement and failure notification.

    Prefers the structural stop (the level that triggered the entry, floored at break-even);
    falls back to TP1 - tp1_runner_sl_buffer x R for signals with no key level behind them.

    With `use_extended_runner_tiers` enabled, the buffer anchors to whichever TP level was
    just hit (TP1 -> TP1.5 -> TP2) instead of always TP1, and the result never loosens the SL
    already in place — each further partial exit only ever tightens the runner's stop.

    The buffer itself scales with the anchor's own R-multiple (1.0 at TP1, 2.0 at TP2, ...)
    so the stop keeps the same proportional headroom below whichever level it is guarding —
    a flat TP1-sized buffer would sit proportionally tighter, and more exposed to a stop-hunt
    wick, against a level further out (e.g. only 15% of TP2's own distance vs 30% of TP1's).
    """
    is_long = pos.direction == Direction.LONG
    tighter = max if is_long else min

    if override_sl is not None:
        new_sl_price = tighter(override_sl, pos.sl) if pos.sl else override_sl
        await _place_runner_sl(pos, remaining, tp_level, new_sl_price, "ladder")
        return

    if not pos.entry_price or pos.entry_price <= 0:
        # Restart-fallback recovery with no audit trail leaves entry=0; every price derived
        # from it is garbage (TCS 2026-10-01: a "runner SL" at 1464 on a 2091 stock).
        logger.warning(
            f"Runner SL for {pos.symbol} skipped: entry price unknown, no sane stop can be derived"
        )
        return

    structural_sl = structural_runner_sl(
        pos.direction, pos.entry_price, pos.tp, getattr(pos, "context", None) or {}
    )

    # The R-based candidate, computed whether or not a structural stop exists so the two can
    # be compared. Without extended tiers it always anchors to TP1, which is the historical rule.
    ratchet_sl = None
    ratchet_level = "TP1"
    if pos.tp and pos.tp > 0:
        risk_distance = abs(pos.tp - pos.entry_price)
        anchor_price = pos.tp
        anchor_multiplier = 1.0
        if settings.use_extended_runner_tiers and tp_level:
            ratcheted_anchor = _tp_level_price(pos, tp_level)
            ratcheted_multiplier = _TP_LEVEL_R_MULTIPLIERS.get((tp_level or "").upper())
            if ratcheted_anchor is not None and ratcheted_multiplier is not None:
                anchor_price = ratcheted_anchor
                ratchet_level = tp_level.upper()
                anchor_multiplier = ratcheted_multiplier
        buffer = settings.tp1_runner_sl_buffer * risk_distance * anchor_multiplier
        ratchet_sl = anchor_price - buffer if is_long else anchor_price + buffer

    if structural_sl is None:
        # Plain ORB breakout: no key level behind the trade, so the R-based rule is the whole
        # answer. Unchanged from the original behaviour, including with the flag off.
        if ratchet_sl is not None:
            anchor_level = ratchet_level
            new_sl_price = ratchet_sl
        else:
            anchor_level = "entry"
            new_sl_price = pos.entry_price
    else:
        anchor_level = "structural"
        new_sl_price = structural_sl
        # structural_runner_sl() is a FLOOR, not the answer. It returns a price for every
        # key-level trigger and is floored at break-even, so taking it verbatim pinned every
        # BREAKOUT runner at entry for the life of the trade and made the ratchet reachable
        # only for plain ORB breakouts — the one strategy it was NOT written for. Harmless
        # while TP2 closed the position outright; a real leak once extended tiers hold 35%
        # past TP2 behind a break-even stop after price has already run 2R.
        # See breakout.md 2026-09-06, Finding 2.
        if settings.use_extended_runner_tiers and ratchet_sl is not None:
            best = tighter(structural_sl, ratchet_sl)
            if best != structural_sl:
                anchor_level = ratchet_level
                new_sl_price = best

    # A stop only ever tightens. Guards the gap-through case too: one 5-min bar can span
    # TP1..TP3 and breakout.pine emits a single alert for the best level reached, so the
    # engine can see TP2 while pos.sl is still the original loss-side stop. Skipped for the
    # entry fallback, which has neither a level nor a TP to reason from and is left exactly
    # as it was.
    have_priced_stop = structural_sl is not None or ratchet_sl is not None
    if settings.use_extended_runner_tiers and have_priced_stop and pos.sl:
        new_sl_price = tighter(new_sl_price, pos.sl)

    await _place_runner_sl(pos, remaining, tp_level, new_sl_price, anchor_level)


async def _place_runner_sl(
    pos, remaining: int, tp_level: str | None, new_sl_price: float, anchor_level: str
) -> None:
    """Place the runner's SL-M at `new_sl_price` and keep the tracked position in step."""
    logger.info(
        f"Runner SL for {pos.symbol}: {anchor_level} {new_sl_price:.2f} "
        f"(entry {pos.entry_price:.2f}, tp_level={tp_level})"
    )
    if not (settings.bracket_enabled and new_sl_price > 0):
        return

    sl_result = await place_sl_order(
        symbol=pos.symbol,
        exchange=pos.exchange,
        direction=pos.direction,
        quantity=remaining,
        sl_price=new_sl_price,
        product=pos.product,
        strategy_tag=pos.strategy,
    )
    if sl_result.status == OrderStatus.SUCCESS:
        pos.sl = new_sl_price  # update tracked SL so time_exit and check_positions stay consistent
        pos.sl_order_id = sl_result.order_id
        logger.info(
            f"SL moved to {anchor_level}-buffer {new_sl_price:.2f} (tp={pos.tp}, buf={settings.tp1_runner_sl_buffer}R) "
            f"for {pos.symbol} remaining {remaining} qty: id={sl_result.order_id}"
        )
    else:
        logger.error(
            f"SL re-placement failed for {pos.symbol} remaining {remaining} qty — "
            "position unprotected until TP1.5/time-exit"
        )
        await notifier.notify_sl_failed(
            pos.symbol,
            f"SL re-placement failed after partial exit: {sl_result.message}",
            strategy=pos.strategy,
        )


async def _handle_exit_order_failure(pos, trade_result) -> None:
    """Exit order could not be placed — reconcile against the broker before alarming.

    The SL may have fired in the window between our SL cancel attempt and this exit
    order (race condition on fast reversals). If already flat, clean up silently.
    """
    logger.error(f"EXIT order failed for {pos.symbol}: {trade_result.message}")
    broker_qty = await tracker.fetch_own_position(
        pos.symbol,
        pos.strategy,
        pos.exchange,
        pos.product,
    )
    if broker_qty == 0:
        # The stop most likely fired while the exit was being retried. Do not unregister and do
        # not record a close of 0.0 here: that dropped a real loss from the ledger and from the
        # daily-loss counter. The poll books it with the stop's own fill and P&L (I4), as it
        # does for any stop hit; the position is released for it and left registered.
        pos.exit_pending = False
        if tracker.find_position(pos.symbol, pos.strategy) is pos:
            logger.info(
                f"EXIT failed but broker position is already 0 for {pos.symbol} "
                "— SL likely fired. Leaving the close for the tracker poll to book."
            )
        else:
            # A position rebuilt from the broker after a restart is never registered, so the poll
            # cannot see it and nothing else would free its slot in the persisted risk store.
            # Release the slot as before. The loss is not booked: there is no stop fill to price
            # it from, and a portfolio-delta estimate is the defect recorded in
            # ACCOUNTING-DEFECT-2026-09-ESTIMATED-EXIT-PNL.md.
            logger.warning(
                f"EXIT failed and the broker is flat for {pos.symbol}, but the position is not "
                "tracked (rebuilt after a restart): releasing its risk slot with no P&L booked"
            )
            risk_engine.record_close(pnl=0.0, strategy=pos.strategy, symbol=pos.symbol)
    else:
        # Position still open (broker_qty > 0 for LONG, < 0 for SHORT, -1 for API error).
        # Clear exit_pending so the next TP/EXIT signal can retry.
        pos.exit_pending = False
        await notifier.notify_exit_failed(pos.symbol, trade_result.message, strategy=pos.strategy)


async def _decline(signal, stage: str, reason: str) -> None:
    """Record a signal the engine refused before sending an order.

    EXITs are recorded too (2026-09-11). They used to be skipped on the reasoning that an
    unmatched EXIT is "a reconciliation problem, not a trade that did not happen" - true, but
    it meant a signal could arrive in a Telegram channel, be acted on by nobody, and leave NO
    TRACE ANYWHERE. Both of that day's BREAKOUT signals were exactly this: an SL HIT for BPCL
    and a TP1 HIT for INDUSINDBK, for positions the engine never held. A strategy-by-strategy
    review built from trades.db reported BREAKOUT as having produced zero signals.

    Counting is protected by the stage/direction on the row, not by dropping the row: a
    review that wants entries only filters `direction IN ('LONG','SHORT')`, which it must do
    anyway to separate entries from exits.
    """
    save_declined(signal, stage=stage, reason=reason)


async def _handle_entry(signal) -> None:
    """Handle a LONG/SHORT entry signal — the existing ORB pipeline.

    Pipeline: symbol rules -> risk gates -> capital -> size -> build_order -> send ->
    bracket -> fill check -> track -> save
    """
    if await _entry_halted(signal):
        return
    if await _entry_rejected_by_symbol_rules(signal):
        return
    if await _entry_rejected_by_trade_policy(signal):
        return
    if not await _entry_passes_risk_gates(signal):
        return

    if await _entry_held_by_unresolved_order(signal):
        return

    capital = await _resolve_entry_capital(signal)
    if capital is None:
        return

    # Use day-start capital for equal risk per trade (cached per-strategy on first
    # fetch of day — each strategy gets its own isolated capital pool)
    sizing_capital = risk_engine.get_sizing_capital(capital, signal.strategy)
    logger.info(f"Capital: live={capital:,.2f} sizing={sizing_capital:,.2f} INR")

    sized = await _resolve_entry_quantity(signal, capital, sizing_capital)
    if sized is None:
        return
    quantity, is_analyze, risk_based_quantity = sized

    rr = _log_entry_sizing(signal, quantity, sizing_capital, risk_based_quantity)
    order = _build_entry_order(signal, quantity, is_analyze)

    # Send to OpenAlgo (routes to live broker or sandbox automatically)
    trade_result = await send_order(order)
    if trade_result.status == OrderStatus.TIMEOUT:
        recovered = await _recover_ambiguous_entry_timeout(signal, order)
        if recovered is not None:
            trade_result = recovered
    await _notify_entry_outcome(signal, trade_result, rr)

    if trade_result.status == OrderStatus.SUCCESS:
        if not await _establish_position(signal, order, quantity, trade_result):
            return

    save(signal, order, trade_result)


async def _entry_halted(signal) -> bool:
    """True if the engine has stopped accepting NEW risk — see mode_guard.

    Two things set the halt, and both mean the same thing for an entry: the conditions this
    trade would be sized and managed under are no longer the ones the engine is configured
    for. A mid-session OpenAlgo mode flip (mode_guard.check_for_flip) leaves the risk
    profile, counter isolation and audit labels pointing at the wrong mode. A dead Telegram
    listener (startup._enter_degraded_mode) means no TP or EXIT alert can arrive either, so
    a new position would be one the engine has no way to be told to close.

    Runs FIRST, before the symbol and risk gates: nothing about a halted engine is worth
    evaluating further. EXIT signals never come through here — closing an open position must
    always stay possible.
    """
    from signal_engine import mode_guard

    if not mode_guard.is_halted():
        return False
    reason = mode_guard.halt_reason()
    logger.critical(f"ENTRY REFUSED (engine halted), skipping {signal.symbol}: {reason}")
    await notifier.notify_risk_limit_hit(reason)
    await _decline(signal, stage="halted", reason=reason)
    return True


async def _entry_rejected_by_symbol_rules(signal) -> bool:
    """True if the symbol itself makes this entry impossible at the broker."""
    product = signal.product or settings.product
    if product != "MIS":
        return False

    # Reject T2T (BE series) symbols for MIS orders — broker will reject immediately.
    exchange = signal.exchange or settings.exchange
    if _is_be_series(signal.symbol, exchange):
        msg = f"{signal.symbol} is T2T (BE series) — MIS not allowed, add to blacklist to suppress"
        logger.warning(msg)
        await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
        await _decline(signal, stage="symbol_rules", reason=msg)
        return True

    # Pre-flight broker reject list — symbols the broker is known to refuse for MIS
    # (GSM, ASM stage IV, F&O ban list, stock-specific overrides). Avoids a wasted slot
    # and a guaranteed rejection round-trip. Maintained in config.yaml broker_restrictions.
    if signal.symbol.upper() in settings.broker_mis_rejected:
        msg = (
            f"{signal.symbol} is on broker MIS reject list (broker_restrictions.flattrade.mis_rejected) "
            "— skipping to avoid certain rejection"
        )
        logger.warning(msg)
        await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
        await _decline(signal, stage="symbol_rules", reason=msg)
        return True

    return False


async def _entry_rejected_by_trade_policy(signal) -> bool:
    """True if the strategy's trade policy (spec item 15) declines this entry.

    Applies to a strategy listed in `runner.strategies` that states an entry cap; a Pine strategy
    that is not listed there is untouched. The counters count FILLED entries and are rebuilt from the
    ledger and the tracker on every call, so a restart cannot reset them and no second copy of the
    state exists. An unreadable ledger declines the entry (a new entry is never taken on a count
    that could not be checked).
    """
    entry = settings.runner.strategies.get(signal.strategy.upper())
    if entry is None or entry.max_entries_per_direction_per_day is None:
        return False
    policy = trade_policy.TradePolicy(
        max_entries_per_direction_per_day=entry.max_entries_per_direction_per_day
    )
    today = datetime.now(IST).date()
    try:
        fills = fetch_entry_fill_directions(signal.strategy, signal.symbol)
    except Exception:
        logger.exception(f"trade policy: cannot read today's entries for {signal.strategy} {signal.symbol}")
        await _decline(signal, stage="trade_policy", reason="today's entries could not be read")
        return True
    held = tracker.find_position(signal.symbol, signal.strategy)
    open_direction = 0 if held is None else (1 if held.direction == Direction.LONG else -1)
    state = trade_policy.rebuild(today, fills, open_direction)
    direction = 1 if signal.direction == Direction.LONG else -1
    verdict = trade_policy.can_arm(policy, state, direction)
    if not verdict:
        return False
    reason = (
        f"{verdict}: {signal.direction.value} {signal.symbol} filled "
        f"{state.long_fills if direction == 1 else state.short_fills} of "
        f"{entry.max_entries_per_direction_per_day} today"
        if verdict == trade_policy.DIRECTION_ALREADY_TAKEN
        else f"{verdict}: {signal.symbol} already has a position"
    )
    logger.warning(f"Trade policy declined {signal.strategy} entry: {reason}")
    await _decline(signal, stage="trade_policy", reason=reason)
    return True


async def _entry_passes_risk_gates(signal) -> bool:
    """Exposure, symbol-concentration and sector-concentration limits."""
    if not risk_engine.check_exposure(signal.strategy):
        reason = risk_engine.exposure_block_reason(signal.strategy)
        logger.warning(f"Risk limit reached, skipping {signal.symbol}: {reason}")
        await notifier.notify_risk_limit_hit(reason)
        await _decline(signal, stage="risk_gates", reason=reason)
        return False

    if not risk_engine.can_trade_symbol(signal.symbol, signal.strategy):
        logger.warning(f"Symbol concentration limit reached for {signal.symbol}")
        await _decline(signal, stage="risk_gates", reason="symbol concentration limit")
        return False

    if not risk_engine.can_trade_sector(signal.symbol, signal.strategy):
        logger.warning(f"Sector concentration limit reached for {signal.symbol}")
        await _decline(signal, stage="risk_gates", reason="sector concentration limit")
        return False

    return True


async def _resolve_entry_capital(signal) -> float | None:
    """Fetch live capital from OpenAlgo. Returns None if the entry cannot be funded."""
    capital = await fetch_available_capital()
    if capital <= 0:
        logger.error("Cannot fetch capital from OpenAlgo, skipping trade")
        await _decline(signal, stage="capital", reason="capital fetch failed or zero")
        return None

    # Minimum capital floor — skip entry if live capital is too depleted.
    # When existing positions consume most of the margin, the margin floor scales qty
    # down to tiny sizes (8-12 shares) that get broker-rejected anyway. Blocking here
    # prevents wasted API calls, avoids consuming the trades_per_day counter, and
    # keeps the remaining capital free for SL exits on open positions.
    if settings.min_capital_for_entry > 0 and capital < settings.min_capital_for_entry:
        msg = (
            f"Insufficient capital for new entry: live={capital:,.0f} < "
            f"min={settings.min_capital_for_entry:,.0f} INR"
        )
        logger.warning(msg)
        await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
        await _decline(signal, stage="capital", reason=msg)
        return None

    return capital


async def _resolve_entry_quantity(
    signal, capital: float, sizing_capital: float
) -> "tuple[int, bool] | None":
    """Size the position and fit it to broker margin.

    Returns (quantity, is_analyze, risk_based_quantity) or None if the trade cannot be
    taken. risk_based_quantity is the pre-margin figure, kept so the sizing log can say when
    the two differ — see _log_entry_sizing().
    """
    quantity = risk_engine.calculate_quantity(signal, capital=sizing_capital)
    risk_based_quantity = quantity
    if quantity <= 0:
        msg = f"Sizing returned 0 for {signal.symbol} — entry price too high for risk budget ({signal.entry:.2f} vs capital={sizing_capital:,.0f})"
        logger.info(msg)
        await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
        await _decline(signal, stage="sizing", reason=msg)
        return None

    # Adjust qty to fit actual broker margin (uses live capital, not day-start)
    # Skip in analyze mode: sandbox has fixed virtual capital, broker margin API is not available
    _, is_analyze = await fetch_trading_mode()
    if is_analyze:
        if not await _sandbox_can_fund(signal, quantity):
            return None
        logger.info(
            f"Analyze mode: skipping margin check for {signal.symbol}, using risk-based qty={quantity}"
        )
    else:
        try:
            quantity = await adjust_qty_for_margin(signal, quantity, capital)
        except MarginAPIError as e:
            msg = f"Margin API failed: {e}"
            logger.error(f"{signal.symbol}: {msg}, skipping trade")
            await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
            await _decline(signal, stage="margin", reason=msg)
            return None
        if quantity <= 0:
            msg = f"Insufficient capital after margin check — {signal.symbol} requires more margin than available (capital={capital:,.0f})"
            logger.warning(msg)
            await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
            return None

    # Apply test qty cap if configured (for minimal exposure live testing)
    if settings.test_qty_cap > 0 and quantity > settings.test_qty_cap:
        logger.info(f"Test qty cap: {quantity} -> {settings.test_qty_cap} for {signal.symbol}")
        quantity = settings.test_qty_cap

    return quantity, is_analyze, risk_based_quantity


def _max_leverage() -> float:
    """Broker-funded leverage ceiling from broker.mis_margin_pct, or 0.0 for "unknown".

    Defensive about the value: this is only used to decide whether to ADD a warning to a log
    line, so a missing or non-numeric setting must degrade to "say nothing" rather than
    raise inside the entry path.
    """
    try:
        margin_pct = float(settings.mis_margin_pct)
    except (TypeError, ValueError):
        return 0.0
    return 1 / margin_pct if margin_pct > 0 else 0.0


def _log_entry_sizing(
    signal, quantity: int, sizing_capital: float, risk_based_quantity: int = 0
) -> float:
    """Emit the one-line sizing audit trail. Returns the reward:risk ratio.

    `risk_based_quantity` is the qty BEFORE adjust_qty_for_margin() scaled it to fit live
    capital. When the two differ the line says so, because the difference silently changes
    what a trade actually risks: the R-multiple stays comparable (it is per-share) but the
    rupee P&L, and therefore every loss-limit counter, stops corresponding to "N full stops"
    — which is the mental model those limits are written in. Defaults to `quantity` for
    callers that never scaled.

    The line also carries implied leverage. Under fixed_fractional the notional is
    risk_per_trade / (sl_pct x (1 + slippage)) and the ENTRY PRICE CANCELS OUT, so at the
    0.20% min_sl_pct BREAKOUT/EMA9/BREAKINGTRADE use it is 4.55x regardless of the stock —
    and at 20% MIS margin one such position needs ~91% of a Rs 35k account. Derivable, but
    nobody derives it mid-session, so it goes in the line and is warned about when the broker
    could not fund it.
    """
    risk_per_share = abs(signal.entry - signal.sl)
    risk_amount = sizing_capital * settings.risk_per_trade
    # Effective SL used for sizing — may be capped if max_sl_pct_for_sizing is set
    effective_sl = risk_per_share
    if settings.max_sl_pct_for_sizing > 0 and signal.entry > 0:
        max_sl_dist = signal.entry * settings.max_sl_pct_for_sizing
        if risk_per_share > max_sl_dist:
            effective_sl = max_sl_dist
    adjusted_rps = effective_sl * (1 + settings.slippage_factor)
    # tp 0 = no take-profit (trailing-stop strategies): no reward figure to report
    reward_per_share = abs(signal.tp - signal.entry) if signal.tp > 0 else 0.0
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0
    pos_value = quantity * signal.entry
    risk_total = quantity * risk_per_share
    cap_note = (
        f" [SL capped {risk_per_share:.2f}->{effective_sl:.2f} for sizing]"
        if effective_sl < risk_per_share
        else ""
    )
    # Margin scaling drift (T1) — only mentioned when it actually happened.
    risk_based_quantity = risk_based_quantity or quantity
    scale_note = (
        f" [margin-scaled {risk_based_quantity} -> {quantity}, "
        f"actual risk {risk_total / sizing_capital:.2%} vs intended "
        f"{settings.risk_per_trade:.2%}]"
        if risk_based_quantity != quantity
        else ""
    )

    # Implied leverage, and whether the product's margin can fund it at all.
    leverage = pos_value / sizing_capital if sizing_capital > 0 else 0.0
    max_leverage = _max_leverage()
    lev_note = ""
    if max_leverage and leverage > max_leverage:
        lev_note = (
            f" WARNING: notional {pos_value:,.0f} is {leverage:.1f}x capital and exceeds "
            f"the {max_leverage:.0f}x allowance at mis_margin_pct={1 / max_leverage:.0%}"
            " - the margin API will cut this order"
        )

    logger.info(
        f"Sizing [{signal.symbol}]: capital={sizing_capital:,.0f} risk={settings.risk_per_trade:.1%}={risk_amount:,.0f} "
        f"entry={signal.entry} sl={signal.sl} tp={signal.tp} "
        f"risk/sh={risk_per_share:.2f}(+{settings.slippage_factor:.0%}slip={adjusted_rps:.2f}){cap_note} "
        f"reward/sh={reward_per_share:.2f} R:R=1:{rr:.1f} "
        f"qty=floor({risk_amount:,.0f}/{adjusted_rps:.2f})={quantity} "
        f"value={pos_value:,.0f} leverage={leverage:.1f}x "
        f"total_risk={risk_total:,.0f}({risk_total / sizing_capital:.2%})"
        f"{scale_note}{lev_note}"
    )
    return rr


def _is_number(value) -> bool:
    """True for a real numeric reading. Excludes bool (True would pass as 1) and any
    stand-in object that merely defines __float__."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


async def _sandbox_can_fund(signal, quantity: int) -> bool:
    """False (and a DECLINED row) when the sandbox plainly cannot fund this order.

    Analyze mode skips the Margin API - the sandbox has no broker to ask - and sizes off the
    fixed sizing.sandbox_capital pool, so nothing in the entry path knew what the sandbox
    actually had left. On 2026-09-11 that meant 14 of 26 validated signals were sent and
    bounced with "Insufficient funds", each landing in trades.db as a broker REJECTION.

    The distinction matters for the paper experiment, not just for tidiness. config.yaml's
    analyze profile removed max_open_positions specifically so a decline would say something
    about the STRATEGY; a drained sandbox put a capacity cap back silently, and labelled its
    refusals as broker errors. Declining here records the real reason and leaves the sizing
    model (equal risk off a fixed pool) untouched.

    An unreadable balance is NOT a refusal - see fetch_funds_available().
    """
    available = await fetch_funds_available()
    # getattr: the gate must degrade to "unknown, allow" on any missing config rather than
    # raising inside the entry path - see _is_number() below.
    margin_pct = getattr(settings, "mis_margin_pct", None)
    sizing_pool = getattr(settings, "sandbox_capital", None)

    # isinstance, not float(): "unknown" must not be mistaken for a real balance. A
    # transient funds-API blip (or a missing config value) must not throw away signals for a
    # reason that has nothing to do with the sandbox being empty - and float() would happily
    # coerce a stand-in object into a plausible-looking number.
    if not all(_is_number(v) for v in (available, margin_pct, sizing_pool)):
        return True

    required = quantity * signal.entry * margin_pct
    if required <= available:
        return True

    msg = (
        f"Sandbox cannot fund this order: needs ~{required:,.0f} INR margin "
        f"({quantity} x {float(signal.entry):,.2f} x {margin_pct:.0%}) but the sandbox has "
        f"{available:,.2f} INR left. Sizing still uses the {sizing_pool:,.0f} sandbox_capital "
        "pool, so this is a CAPACITY limit, not a signal-quality one - top the sandbox up to "
        "keep the paper sample complete."
    )
    logger.warning(f"{signal.symbol}: {msg}")
    await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
    await _decline(signal, stage="sandbox_margin", reason=msg)
    return False


def _build_entry_order(signal, quantity: int, is_analyze: bool):
    """Build the entry order, applying the analyze-mode off-hours product override.

    In analyze mode with off-hours testing enabled, override MIS→CNC to bypass the
    sandbox after-hours restriction (sandbox blocks new MIS orders outside the
    09:00–squareoff window).
    """
    off_hours_product_override = (
        is_analyze
        and settings.allow_off_hours_testing
        and (signal.product or settings.product) == "MIS"
    )
    if off_hours_product_override:
        logger.info(f"Off-hours testing: overriding product MIS→CNC for {signal.symbol}")
    return build_order(signal, quantity, product="CNC" if off_hours_product_override else "")


async def _recover_ambiguous_entry_timeout(signal, order) -> "TradeResult | None":
    """Check whether an entry order that TIMED OUT/ERRORed client-side actually filled.

    send_order's TIMEOUT/ERROR only means the HTTP response never arrived in time — not
    that OpenAlgo/the broker never processed the order. 2026-09-24: NAUKRI and SOLARINDS
    both timed out on entry, yet their orders showed status=complete on the broker's own
    orderbook ~6-9s later. _handle_entry treated the timeout as "no position" and never
    called _establish_position — so both fills sat on the broker completely untracked: no
    SL, no TP monitoring, no risk-slot accounting, invisible to the engine and to the
    trader. Poll the orderbook for a completed order matching this signal before
    conceding the entry genuinely never happened.

    Matches on symbol + action + quantity + order_status=complete, skipping order ids this
    engine already booked today and rows tagged for another strategy. The triple alone is
    NOT unique: a same-day re-entry at the same size (ADANIPORTS 10:50 then 11:20,
    2026-10-01) matches the earlier, closed fill. Orderbook rows don't carry a timestamp
    consistent enough across brokers to filter on instead.
    """
    action = order.action.value
    # Orders already booked today for this symbol (an earlier entry that has since closed
    # and left the tracker). ADANIPORTS 2026-10-01: the 11:20 re-entry timed out and the
    # 10:50 entry (same symbol, side and quantity) was adopted in its place.
    booked_ids = fetch_order_ids_today(signal.symbol)
    since = datetime.now(IST).replace(microsecond=0)
    last_read_clean = False
    for attempt in range(1, settings.entry_timeout_recovery_attempts + 1):
        await asyncio.sleep(settings.entry_timeout_recovery_delay_seconds)
        orderbook = await fetch_orderbook()
        if orderbook is None:
            last_read_clean = False
            continue
        last_read_clean = True
        for o in orderbook:
            if not isinstance(o, dict):
                continue
            if str(o.get("symbol", "")).upper() != signal.symbol.upper():
                continue
            if str(o.get("action", "")).upper() != action:
                continue
            try:
                if int(o.get("quantity", 0)) != order.quantity:
                    continue
            except (TypeError, ValueError):
                continue
            status = str(o.get("order_status") or o.get("status") or "").lower()
            if status not in ("complete", "filled"):
                if status not in ("rejected", "cancelled", "canceled", "expired"):
                    last_read_clean = False  # still working or partly filled: not a clean "no"
                continue
            order_id = str(o.get("orderid") or o.get("order_id") or "")
            if not order_id:
                continue
            if order_id in booked_ids:
                continue  # already booked earlier today — an old fill, not this signal's
            row_strategy = str(o.get("strategy") or "")
            if row_strategy and row_strategy.upper() != str(signal.strategy).upper():
                continue  # another strategy's order on the same symbol
            if any(p.entry_order_id == order_id for p in tracker._positions.values()):
                continue  # already tracked under some other signal — not this one's fill
            logger.warning(
                f"Entry order timeout for {signal.symbol} was ambiguous — order DID fill on "
                f"the broker (id={order_id}, attempt {attempt}/"
                f"{settings.entry_timeout_recovery_attempts}) — recovering the position that "
                "would otherwise have been orphaned"
            )
            return TradeResult(
                order_id=order_id, status=OrderStatus.SUCCESS, message="recovered after timeout"
            )
    if not last_read_clean:
        return await _hold_uncertain_entry(signal, order, since)
    logger.info(
        f"Entry timeout recovery for {signal.symbol}: no matching completed order found "
        f"after {settings.entry_timeout_recovery_attempts} checks — treating as a genuine failure"
    )
    return None


async def _hold_uncertain_entry(signal, order, since: datetime) -> TradeResult:
    """The entry's fate is unknown (book unreadable or the order still working): block new
    orders for this strategy and symbol until it is reconciled, and alert.

    KALYANKJIL 2026-10-13: a timed-out BUY 188 filled on the broker and nothing tracked it.
    """
    order_reconcile.mark_uncertain(
        order_reconcile.UncertainOrder(
            strategy=signal.strategy, symbol=signal.symbol, action=order.action.value,
            quantity=order.quantity, since=since, kind="ENTRY",
        )
    )
    message = (
        f"UNCERTAIN: {order.action.value} {order.quantity} {signal.symbol} [{signal.strategy}] "
        "entry timed out and the broker order book could not confirm it; no further orders for "
        "this symbol until it is reconciled"
    )
    logger.critical(f"ENTRY {message}")
    await notifier.notify(
        f"ORDER UNCONFIRMED: {message}. The order may be live at the broker with no stop: check now."
    )
    return TradeResult(status=OrderStatus.TIMEOUT, message=message)


async def _entry_held_by_unresolved_order(signal) -> bool:
    """True when an earlier order for this strategy and symbol is still unconfirmed.

    Reconciles first. A completed or absent order releases the hold; a completed one is
    alerted (the engine does not track it; an exit signal or the time exit will manage it).
    """
    pending = order_reconcile.get_uncertain(signal.strategy, signal.symbol)
    if pending is None:
        return False
    result = await order_reconcile.reconcile_timed_out_order(
        symbol=pending.symbol, action=pending.action, quantity=pending.quantity,
        since=pending.since, fetch_orderbook=fetch_orderbook,
        attempts=settings.entry_timeout_recovery_attempts,
        delay=settings.entry_timeout_recovery_delay_seconds,
    )
    if result.outcome == order_reconcile.UNCERTAIN:
        logger.critical(
            f"ENTRY blocked for {signal.symbol} [{signal.strategy}]: an earlier {pending.kind} "
            f"order is still unconfirmed ({result.detail})"
        )
        return True
    order_reconcile.clear_uncertain(signal.strategy, signal.symbol)
    if result.outcome == order_reconcile.FILLED:
        logger.critical(
            f"ENTRY blocked for {signal.symbol} [{signal.strategy}]: the earlier {pending.kind} "
            f"order {result.order_id} completed at the broker and is not tracked by the engine"
        )
        await notifier.notify(
            f"UNTRACKED FILL: {pending.action} {pending.quantity} {signal.symbol} [{signal.strategy}] "
            f"order {result.order_id} completed after a timeout. The engine has no stop for it: "
            "check the broker; an exit signal or the time exit will close it."
        )
        return True
    return False


async def _notify_entry_outcome(signal, trade_result, rr: float) -> None:
    """Announce the broker's response to the entry order."""
    if trade_result.status == OrderStatus.SUCCESS:
        logger.info(f"Order placed for {signal.symbol}: id={trade_result.order_id}")
        # +1 because risk_engine.record_trade runs below — this slot is now taken.
        slot_context = notifier.format_slot_context(
            risk_engine.open_positions_for(signal.strategy) + 1,
            risk_engine.effective_max_open_positions_for(signal.strategy),
        )
        await notifier.notify_order_placed(
            signal.symbol,
            signal.direction.value,
            strategy=signal.strategy,
            signal_price=signal.entry,
            sl=signal.sl,
            tp=signal.tp if signal.tp > 0 else None,  # 0 = no take-profit, not shown
            rr=rr if signal.tp > 0 else None,
            slot_context=slot_context,
        )
    else:
        logger.warning(
            f"Order {trade_result.status.value} for {signal.symbol}: {trade_result.message}"
        )
        await notifier.notify_order_rejected(
            signal.symbol, trade_result.message, strategy=signal.strategy
        )


async def _establish_position(signal, order, quantity: int, trade_result) -> bool:
    """Record the trade, protect it with an SL, and register it for tracking.

    Returns False when the execution guard closed the position (a bad fill). The entry row
    and the guard's exit row are then already saved here, entry first so the entry never reads
    as an open position, and the caller must skip its own save. If the guard's close order
    failed and the position is still open, the stop is restored and this carries on as a
    normal tracked entry.
    """
    risk_engine.record_trade(strategy=signal.strategy, symbol=signal.symbol)
    logger.info(f"Capacity: {risk_engine.capacity_status(signal.strategy)}")

    sl_order_id = await _place_entry_bracket(signal, quantity, trade_result.order_id)
    entry_fill_price = await _fetch_entry_fill(signal, trade_result.order_id)
    # Attach it to the result so db.save persists it. This is the only moment the fill
    # price is cheaply knowable: the broker tradebook is wiped daily, so a number not
    # captured here is gone for good and slippage becomes unmeasurable after the fact.
    trade_result.fill_price = entry_fill_price

    guard = await _auto_close_on_bad_fill(
        signal, quantity, entry_fill_price, sl_order_id, trade_result.order_id
    )
    if guard is not None and guard.closed:
        save(signal, order, trade_result)
        _save_bad_fill_exit(signal, quantity, trade_result.order_id, entry_fill_price, guard)
        return False
    if guard is not None:
        sl_order_id = guard.replacement_sl_order_id

    # Always notify LIVE so trader knows position is active and SL is protecting it.
    # fill_price=0 triggers "fill pending" wording in the message.
    await notifier.notify_entry_filled(
        signal.symbol,
        signal.direction.value,
        entry_fill_price or 0.0,
        quantity,
        signal.entry,
        strategy=signal.strategy,
        sl=signal.sl,
        tp=signal.tp if signal.tp > 0 else None,  # 0 = no take-profit, not shown
    )

    tracker.register(
        TrackedPosition(
            symbol=signal.symbol,
            strategy=signal.strategy,
            exchange=signal.exchange or settings.exchange,
            product=signal.product or settings.product,
            entry_price=signal.entry,
            quantity=quantity,
            sl=signal.sl,
            tp=signal.tp,
            direction=signal.direction,
            entry_order_id=trade_result.order_id,
            sl_order_id=sl_order_id,
            fill_price=entry_fill_price or 0.0,
            context=signal.context or {},
            sig_id=signal.sig_id or "",
        )
    )
    return True


async def _place_entry_bracket(signal, quantity: int, entry_order_id: str) -> str:
    """Place the SL bracket leg (MIS only). Returns the SL order id, or "" if none.

    TP is NOT placed as a broker order — Indian brokers treat a second SELL as a new
    short, causing FUND LIMIT INSUFFICIENT. TP exit is driven by TradingView TP HIT
    signal -> _handle_exit pipeline. SL-M placed here as broker-side safety net.
    CNC: SL-M cancelled at EOD by NSE, no GTT in OpenAlgo — skip bracket entirely.
    CNC exits rely on TradingView EXIT alerts (close < 200 SMA, max hold days).
    """
    product = signal.product or settings.product
    skip_cnc_bracket = product == "CNC" and not settings.bracket_cnc_sl_enabled
    if skip_cnc_bracket:
        logger.info(
            f"Skipping SL bracket for {signal.symbol}: CNC product, "
            "SL-M cancelled at EOD by NSE (bracket.cnc_sl_enabled=false)"
        )
    if not (settings.bracket_enabled and not skip_cnc_bracket):
        return ""

    sl_result, _ = await send_bracket_legs(signal, quantity, entry_order_id)
    sl_order_id = sl_result.order_id if sl_result else ""
    if sl_result and sl_result.status == OrderStatus.SUCCESS:
        await notifier.notify_sl_placed(
            signal.symbol, sl_order_id, strategy=signal.strategy, sl_price=signal.sl
        )
    else:
        await notifier.notify_sl_failed(
            signal.symbol, sl_result.message if sl_result else "no result", strategy=signal.strategy
        )
    return sl_order_id


async def _fetch_entry_fill(signal, entry_order_id: str) -> float | None:
    """Fetch the actual entry fill price and log the slippage against the signal."""
    entry_fill_price = await fetch_order_fill_price(entry_order_id, signal.strategy)
    if entry_fill_price:
        slippage = entry_fill_price - signal.entry
        logger.info(
            f"Entry fill: {signal.symbol} avg_price={entry_fill_price:.2f} "
            f"(signal={signal.entry:.2f}, slippage={slippage:+.2f})"
        )
    else:
        logger.warning(f"Entry fill price unavailable for {signal.symbol} id={entry_order_id}")
    return entry_fill_price


def entry_slippage_r(signal, entry_fill_price) -> float:
    """Adverse entry slippage in units of the signal's risk (|entry - sl|).

    Positive means the fill was worse than the signal price (paid up on a long, sold lower on
    a short); negative means it was better. 0.0 when the fill or the risk distance is unknown.
    """
    direction = 1 if signal.direction == Direction.LONG else -1
    return trade_policy.entry_slippage_r(direction, signal.entry, signal.sl, entry_fill_price)


@dataclass(frozen=True)
class BadFillClose:
    """What the execution guard did about a bad entry fill.

    closed is False when the close order did not go through and the position is still open at
    the broker; replacement_sl_order_id is then the stop that was restored so the caller can
    carry on tracking it as a normal protected entry.
    """

    closed: bool
    exit_type: str
    reason: str
    slippage_r: float
    close_order_id: str = ""
    exit_fill_price: float | None = None
    pnl: float = 0.0
    replacement_sl_order_id: str = ""


async def _auto_close_on_bad_fill(
    signal, quantity: int, entry_fill_price, sl_order_id: str, entry_order_id: str = ""
) -> BadFillClose | None:
    """Close immediately if the fill is unusable. None when the fill is acceptable.

    Two cases: the fill landed past TP (negative reward, disproportionately wide SL), or
    adverse slippage exceeded max_entry_slippage_r. The second matters because TP, the runner
    stop and every R figure are anchored to the SIGNAL price, not the fill: TCS 2026-09-30
    filled 0.83R above the signal, so the "TP1 hit" partial exit sold below cost and the
    runner stop (2090.21) sat under the real entry (2091.00). max_entry_slippage_r = 0 disables
    the slippage case.

    The close order's result is checked: if it did not go through and the broker still shows
    the position open, the stop that was cancelled to make room for the close is restored and
    the position stays tracked (closed=False).
    """
    verdict = trade_policy.bad_fill(
        1 if signal.direction == Direction.LONG else -1,
        signal.entry,
        signal.sl,
        signal.tp,
        entry_fill_price,
        settings.max_entry_slippage_r,
    )
    if not verdict.violated:
        return None
    fill_overshot_tp = verdict.kind == trade_policy.TP_OVERSHOT
    slippage_r = verdict.slippage_r

    if fill_overshot_tp:
        exit_type = EXIT_ENTRY_REJECTED_TP_OVERSHOT
        reason = f"fill {entry_fill_price:.2f} overshot TP {signal.tp:.2f} - auto-closed"
    elif verdict.kind == trade_policy.STOP_BREACHED:
        exit_type = EXIT_ENTRY_REJECTED_STOP_BREACHED
        reason = (
            f"fill {entry_fill_price:.2f} is at or beyond the stop {signal.sl:.2f} "
            "(gap through the stop) - auto-closed"
        )
    else:
        exit_type = EXIT_ENTRY_REJECTED_SLIPPAGE
        reason = (
            f"fill {entry_fill_price:.2f} slipped {slippage_r:.2f}R from signal "
            f"{signal.entry:.2f} (max {settings.max_entry_slippage_r:.2f}R) - auto-closed"
        )
    logger.error(
        f"Bad entry fill for {signal.symbol}: {reason} direction={signal.direction.value}"
    )
    sl_cancelled = await cancel_order(sl_order_id, signal.strategy) if sl_order_id else True
    close_order = build_exit_order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        quantity=quantity,
        product=signal.product or settings.product,
        strategy_tag=signal.strategy,
        direction=signal.direction,
    )
    # Broker order timestamps are whole seconds; keeping microseconds here would make a close
    # stamped in the same second as the send look older than it and be skipped.
    close_started = datetime.now(IST).replace(microsecond=0)
    close_result = await send_order(close_order)
    if close_result.status == OrderStatus.TIMEOUT:
        # A close the broker accepted may not have filled yet when the position is read back,
        # which would read as "still open". The orderbook shows it, and gives the ledger the
        # real order id.
        close_result = await _find_recent_market_exit_fill(close_order, close_started) or close_result
    if close_result.status != OrderStatus.SUCCESS and await _position_still_open(signal, close_order):
        return await _restore_protection_after_failed_close(
            signal, quantity, entry_order_id, close_result, exit_type, reason, slippage_r,
            sl_order_id, sl_cancelled,
        )

    exit_fill, pnl = await _bad_fill_close_pnl(signal, quantity, entry_fill_price, close_result)
    risk_engine.record_close(pnl, strategy=signal.strategy, symbol=signal.symbol)
    await notifier.notify_order_rejected(
        signal.symbol,
        reason,
        strategy=signal.strategy,
    )
    return BadFillClose(
        closed=True,
        exit_type=exit_type,
        reason=reason,
        slippage_r=slippage_r,
        close_order_id=close_result.order_id,
        exit_fill_price=exit_fill,
        pnl=pnl,
    )


async def _position_still_open(signal, close_order) -> bool:
    """True unless the broker confirms this strategy is flat on the symbol.

    A close that timed out may still have filled. None (broker unreachable) counts as open:
    the safe assumption is that the position needs protection and tracking.
    """
    broker_qty = await tracker.fetch_own_position(
        signal.symbol, signal.strategy, close_order.exchange, close_order.product
    )
    return broker_qty != 0


async def _bad_fill_close_pnl(
    signal, quantity: int, entry_fill_price, close_result
) -> tuple[float | None, float]:
    """(exit fill, realised P&L) for a bad-fill close, from the two confirmed fills.

    Falls back to the broker's realised delta, as the signal-driven exit does, when the close
    order's own fill cannot be read; that figure can include other closes.
    """
    exit_fill = None
    if close_result.order_id:
        exit_fill = await fetch_order_fill_price(close_result.order_id, signal.strategy)
    if exit_fill and entry_fill_price:
        return exit_fill, leg_pnl(signal.direction, entry_fill_price, exit_fill, quantity)
    pnl_delta, _ = await _book_realised_pnl_delta()
    logger.warning(
        f"Bad-fill close fill unavailable for {signal.symbol}: booking the broker realised "
        f"delta {pnl_delta:,.2f} instead (may include other closes)"
    )
    return None, pnl_delta


async def _restore_protection_after_failed_close(
    signal, quantity: int, entry_order_id: str, close_result, exit_type: str, reason: str,
    slippage_r: float, sl_order_id: str, sl_cancelled: bool,
) -> BadFillClose:
    """The guard's close order failed and the position is open: put the stop back and alert.

    The stop was cancelled to make room for the close, so without this the position would sit
    at the broker unprotected. Restoring it hands the position back to the normal tracked
    path. Only called when the broker shows the position open, never after a close that went
    through, where a restored stop would open a new short. If the stop's cancel itself failed
    the original is still live, and a second full-size stop would leave two that could both
    trigger, so the original is kept instead.
    """
    logger.critical(
        f"Bad-fill close FAILED for {signal.symbol} ({close_result.status.value}: "
        f"{close_result.message}); position still open - keeping it tracked and protected"
    )
    if sl_cancelled:
        sl_order_id = await _place_entry_bracket(signal, quantity, entry_order_id)
    attempted = reason.replace("auto-closed", "auto-close attempted")
    await notifier.notify_exit_failed(
        signal.symbol, f"{attempted}; close order failed: {close_result.message}",
        strategy=signal.strategy,
    )
    return BadFillClose(
        closed=False,
        exit_type=exit_type,
        reason=reason,
        slippage_r=slippage_r,
        replacement_sl_order_id=sl_order_id,
    )


def _save_bad_fill_exit(signal, quantity: int, entry_order_id: str, entry_fill, outcome) -> None:
    """File the guard's close in trades.db as its own exit type (the entry row is saved first)."""
    adverse_per_share = (
        (entry_fill - signal.entry) if signal.direction == Direction.LONG
        else (signal.entry - entry_fill)
    ) if entry_fill else 0.0
    save_execution_exit(
        strategy=signal.strategy,
        symbol=signal.symbol,
        signal_entry=signal.entry,
        sl=signal.sl,
        tp=signal.tp,
        quantity=quantity,
        entry_order_id=entry_order_id,
        exit_order_id=outcome.close_order_id,
        entry_fill=entry_fill,
        exit_fill=outcome.exit_fill_price,
        pnl=outcome.pnl,
        exit_type=outcome.exit_type,
        reason=outcome.reason,
        slippage_r=outcome.slippage_r,
        slippage_rs=adverse_per_share * quantity,
        sig_id=signal.sig_id,
    )


async def handle_message(text: str) -> None:
    """Full sequential pipeline for a single signal message.

    Dispatches to _handle_entry (LONG/SHORT) or _handle_exit (EXIT).
    """
    # 1. Normalize and parse
    normalized = normalize(text)
    signal = parse(normalized)
    if signal is None:
        logger.debug("Unparseable message, skipping")
        return

    # Single-source guard: a strategy whose signals come from the Python runner must not also
    # trade off its Pine alerts (entries AND exits), or every trade would be duplicated.
    if single_source.should_drop_alert(settings.runner, signal.strategy):
        dropped = single_source.record_drop(signal.strategy)
        logger.warning(
            f"Dropped Pine alert for {signal.strategy} {signal.direction.value} "
            f"{signal.symbol}: signal_source is python ({dropped} dropped so far)"
        )
        return

    # Parity guard: while a Python strategy supersedes this Pine tag on the symbol, a Pine ENTRY would be a
    # duplicate paper trade. The alert is kept as a declined row for the comparison. A superseded tag's EXIT passes only when the engine tracks a position for it.
    if signal.direction != Direction.EXIT and single_source.pine_superseded(
        settings.runner, signal.strategy, signal.symbol
    ):
        logger.warning(
            f"Pine entry not traded for {signal.strategy} {signal.direction.value} {signal.symbol}: "
            "the Python strategy is the sole execution source"
        )
        await _decline(
            signal, stage="superseded", reason="the Python strategy is the sole execution source on this symbol"
        )
        return

    # A superseded Pine tag never opens a position on this symbol, so an EXIT with no tracked position can
    # only concern a trade the engine does not own. Letting it reach broker recovery rebuilt a position from
    # the net broker quantity and sent an order (2026-10-13 HINDALCO: BUY 320, an unmanaged long).
    if (
        signal.direction == Direction.EXIT
        and single_source.pine_superseded(settings.runner, signal.strategy, signal.symbol)
        and tracker.find_position(signal.symbol, signal.strategy) is None
    ):
        logger.warning(
            f"Pine exit not acted on for {signal.strategy} {signal.symbol}: the Python strategy owns "
            "this symbol and the engine holds no position for the Pine tag"
        )
        await _decline(
            signal, stage="superseded", reason="exit for a superseded Pine tag with no tracked position"
        )
        return

    logger.bind(symbol=signal.symbol).info(
        f"Parsed signal: {signal.strategy} {signal.direction.value} {signal.symbol} "
        f"entry={signal.entry} sl={signal.sl} tp={signal.tp}"
    )
    await dispatch_signal(signal)


async def dispatch_signal(signal: Signal) -> None:
    """Validate a parsed Signal and route it to the entry or exit path.

    Shared by handle_message (Telegram text) and the Python runner (real Signal objects),
    so both sources pass through exactly the same gates.
    """
    # 1b. Freshness. Source-independent and ENTRY-only: a signal that has aged past
    # stale_signal_seconds by the time it gets here - for any reason - must never open a
    # position. EXITs are exempt: closing risk must always stay possible.
    origin = freshness.current_origin()
    if origin is not None and signal.direction != Direction.EXIT:
        age = freshness.stale_age(origin, settings.stale_signal_seconds)
        if age is not None:
            reason = f"stale by {age:.0f}s (limit {settings.stale_signal_seconds}s)"
            logger.warning(f"Refusing stale entry {signal.strategy} {signal.symbol}: {reason}")
            await _decline(signal, stage="stale", reason=reason)
            return

    # 2. Validate
    result = validate(signal)
    if result.status != ValidationStatus.VALID:
        logger.info(f"Signal {result.status.value}: {result.reason}")
        await _decline(signal, stage=f"validator:{result.status.value}", reason=result.reason or "")
        return

    # 3. Dispatch based on direction.
    # contextualize tags every line emitted downstream with the symbol, so a whole
    # trade can be pulled out of the log with a single grep.
    with logger.contextualize(symbol=signal.symbol):
        if signal.direction == Direction.EXIT:
            await _handle_exit(signal)
        else:
            await _handle_entry(signal)


def main() -> None:
    """Entry point — start the signal engine with position tracker."""
    setup_logger()

    if startup.run_health_check_cli(sys.argv):
        return
    if startup.run_test_signal_cli(sys.argv, handle_message):
        return

    startup.log_startup_banner()
    startup.start_engine(risk_engine, tracker, handle_message, dispatch_signal)


if __name__ == "__main__":
    main()
