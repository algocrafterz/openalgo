"""Entry point and pipeline orchestration for the signal engine."""

import asyncio
import math
import os
import sqlite3
import sys
from datetime import datetime

from loguru import logger

from signal_engine.api_client import cancel_order, fetch_available_capital, fetch_open_position, fetch_order_fill_price, fetch_order_status, fetch_realised_pnl, fetch_trading_mode, fetch_margin, MarginAPIError
from signal_engine.config import settings
from signal_engine.db import fetch_last_entry_trade, save
from signal_engine.executor import build_exit_order, build_order, place_sl_order, send_bracket_legs, send_order
from signal_engine.logger_setup import setup_logger
from signal_engine.models import Direction, OrderStatus, TradeResult, ValidationStatus
from signal_engine import notifier
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.runtime import build_risk_engine
from signal_engine.risk_store import RiskStore, RISK_DB_PATH
from signal_engine import startup
from signal_engine.tracker import PositionTracker, TrackedPosition, TradeRecord, _compute_r
from signal_engine.validator import validate
from signal_engine.timeutils import IST


_OPENALGO_DB = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "db", "openalgo.db")
)


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


def _get_exit_lock(symbol: str, strategy: str) -> asyncio.Lock:
    key = f"{symbol}:{strategy}"
    if key not in _exit_locks:
        _exit_locks[key] = asyncio.Lock()
    return _exit_locks[key]


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
    - TP2 or unknown → None (no further standard TP level)

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
        await _handle_exit_locked(signal)


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

    await _cancel_sl_before_exit(pos)

    exit_order, trade_result = await _place_exit_order(pos, exit_qty)

    if trade_result.status == OrderStatus.SUCCESS:
        if not await _book_exit_result(signal, pos, tp_level, exit_qty, is_full_exit, trade_result):
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
    api_qty = await fetch_open_position(signal.symbol, signal.strategy, exchange, product)
    if api_qty == 0 or api_qty == -1:
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
    risk_engine.record_rejection(symbol=pos.symbol)
    tracker.unregister(signal.symbol, signal.strategy)
    await notifier.notify_orphaned_position(
        pos.symbol, pos.strategy, pos.direction.value,
        pos.entry_order_id, f"exit blocked: entry order {status_lower}",
    )
    return True


async def _reconcile_sl_hit(signal, pos) -> None:
    """Book a close the broker SL-M already executed.

    Do NOT place another SELL (would create naked short). Just clean up tracker.
    """
    logger.info(f"EXIT: SL HIT reconcile for {pos.symbol} — no broker order placed, cleaning up tracker")
    if pos.sl_order_id:
        await cancel_order(pos.sl_order_id, pos.strategy)
    pnl_delta, current_realised = await _book_realised_pnl_delta()
    await tracker.book_close(
        pos,
        pnl_delta=pnl_delta,
        exit_price=pos.sl,
        exit_types=["SL"],
        hold_minutes=_hold_minutes(pos),
        max_trades=settings.max_trades_per_day,
        new_realised_pnl=current_realised,
    )
    tracker.unregister(signal.symbol, signal.strategy)
    risk_engine.record_close(pnl=pnl_delta, symbol=pos.symbol)
    if not tracker._positions:
        await tracker.send_day_summary()


async def _cancel_sl_before_exit(pos) -> None:
    """ALWAYS cancel SL before placing an exit order.

    Indian brokers treat any SELL while SL SELL is active as a new SHORT position
    (FUND LIMIT INSUFFICIENT). SL must be cancelled first, even for partial exits.
    """
    if not pos.sl_order_id:
        return
    success = await cancel_order(pos.sl_order_id, pos.strategy)
    if success:
        logger.info(f"EXIT: SL order {pos.sl_order_id} cancelled for {pos.symbol}")
    else:
        logger.warning(f"EXIT: failed to cancel SL {pos.sl_order_id} for {pos.symbol}")


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
    """
    trade_result = TradeResult(status=OrderStatus.ERROR, message="Not attempted")
    for _attempt in range(1, settings.bracket_tp_exit_retries + 1):
        trade_result = await send_order(exit_order)
        if trade_result.status == OrderStatus.SUCCESS:
            break
        logger.warning(
            f"EXIT attempt {_attempt}/{settings.bracket_tp_exit_retries} failed for "
            f"{pos.symbol}: {trade_result.message}"
        )
        if _attempt < settings.bracket_tp_exit_retries:
            await asyncio.sleep(settings.bracket_retry_delay)
    return trade_result


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
    logger.info(f"EXIT PnL for {pos.symbol}: delta={pnl_delta:,.2f} (realised={current_realised:,.2f})")

    base_price = pos.fill_price or pos.entry_price
    hold_min = _hold_minutes(pos)
    # Use the signal's TP price as approximate exit price — MARKET order fills at ~TP.
    approx_exit_price = signal.tp if signal.tp and signal.tp > 0 else None

    if is_full_exit:
        # book_close advances the day counters and the realised-P&L snapshot.
        await _finalize_full_exit(
            signal, pos, tp_level, pnl_delta, hold_min,
            approx_exit_price, current_realised,
        )
        if not tracker._positions:
            await tracker.send_day_summary()
        return True

    remaining = pos.quantity - exit_qty
    if remaining <= 0:
        await _finalize_invalid_partial(
            signal, pos, tp_level, exit_qty, pnl_delta, current_realised,
            hold_min, approx_exit_price,
        )
        return False

    await _finalize_partial_exit(
        pos, tp_level, exit_qty, remaining, pnl_delta,
        base_price, hold_min,
    )
    # Partial exit: accumulate P&L only — the trade is counted at its final close.
    tracker.record_exit(
        pnl=pnl_delta, is_partial=True, new_realised_pnl=current_realised,
    )
    return True


async def _finalize_full_exit(
    signal, pos, tp_level, pnl_delta: float, hold_min: int,
    approx_exit_price, current_realised: float,
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
    risk_engine.record_close(pnl=pnl_delta, symbol=pos.symbol)
    # exit_pending does not need clearing — position is unregistered


async def _finalize_invalid_partial(
    signal, pos, tp_level, exit_qty: int, pnl_delta: float, current_realised: float,
    hold_min: int, approx_exit_price,
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
    risk_engine.record_close(pnl=pnl_delta, symbol=pos.symbol)
    if not tracker._positions:
        await tracker.send_day_summary()


async def _finalize_partial_exit(
    pos, tp_level, exit_qty: int, remaining: int, pnl_delta: float,
    base_price: float, hold_min: int,
) -> None:
    """Reduce the tracked qty, re-protect the runner, and notify."""
    pos.quantity = remaining
    pos.sl_order_id = ""  # clear old SL id — will be updated below if re-placement succeeds
    logger.info(f"Partial exit: {pos.symbol} exited {exit_qty}, remaining {remaining}")

    await _replace_runner_sl(pos, remaining)

    # R for this partial leg only (shows how far into the trade we are)
    r_partial = _compute_r(pnl_delta, exit_qty, base_price, pos.sl)
    next_tp_info = compute_next_tp(pos, tp_level)
    next_tp_label = next_tp_info[0] if next_tp_info else None
    next_tp_price = next_tp_info[1] if next_tp_info else None
    await notifier.notify_partial_exit(
        pos.symbol, exit_qty, remaining, tp_level or "", pnl_delta,
        strategy=pos.strategy, new_sl=pos.sl,
        next_tp_label=next_tp_label, next_tp_price=next_tp_price,
        direction=pos.direction.value, r_multiple=r_partial,
        entry_price=base_price, hold_minutes=hold_min,
    )
    # Accumulate partial P&L and record exit label for final TradeRecord
    pos.realized_pnl += pnl_delta
    pos.exit_types.append(tp_level or "TP")
    pos.exit_pending = False  # partial exit done — allow next TP signal


async def _replace_runner_sl(pos, remaining: int) -> None:
    """Re-place SL at TP1 - 0.1R after a partial TP exit (50-50 TP booking strategy)."""
    if pos.tp and pos.tp > 0:
        risk_distance = abs(pos.tp - pos.entry_price)
        buffer = settings.tp1_runner_sl_buffer * risk_distance
        if pos.direction == Direction.LONG:
            new_sl_price = pos.tp - buffer
        else:
            new_sl_price = pos.tp + buffer
    else:
        new_sl_price = pos.entry_price
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
            f"SL moved to TP1-buffer {new_sl_price:.2f} (tp={pos.tp}, buf={settings.tp1_runner_sl_buffer}R) "
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
    broker_qty = await fetch_open_position(
        pos.symbol, pos.strategy,
        pos.exchange, pos.product,
    )
    if broker_qty == 0:
        logger.info(
            f"EXIT failed but broker position is already 0 for {pos.symbol} "
            "— SL likely fired. Cleaning up tracker."
        )
        tracker.unregister(pos.symbol, pos.strategy)
        risk_engine.record_close(pnl=0.0, symbol=pos.symbol)
    else:
        # Position still open (broker_qty > 0 for LONG, < 0 for SHORT, -1 for API error).
        # Clear exit_pending so the next TP/EXIT signal can retry.
        pos.exit_pending = False
        await notifier.notify_exit_failed(pos.symbol, trade_result.message, strategy=pos.strategy)


async def _handle_entry(signal) -> None:
    """Handle a LONG/SHORT entry signal — the existing ORB pipeline.

    Pipeline: symbol rules -> risk gates -> capital -> size -> build_order -> send ->
    bracket -> fill check -> track -> save
    """
    if await _entry_rejected_by_symbol_rules(signal):
        return
    if not await _entry_passes_risk_gates(signal):
        return

    capital = await _resolve_entry_capital(signal)
    if capital is None:
        return

    # Use day-start capital for equal risk per trade (cached on first fetch of day)
    sizing_capital = risk_engine.get_sizing_capital(capital)
    logger.info(f"Capital: live={capital:,.2f} sizing={sizing_capital:,.2f} INR")

    sized = await _resolve_entry_quantity(signal, capital, sizing_capital)
    if sized is None:
        return
    quantity, is_analyze = sized

    rr = _log_entry_sizing(signal, quantity, sizing_capital)
    order = _build_entry_order(signal, quantity, is_analyze)

    # Send to OpenAlgo (routes to live broker or sandbox automatically)
    trade_result = await send_order(order)
    await _notify_entry_outcome(signal, trade_result, rr)

    if trade_result.status == OrderStatus.SUCCESS:
        if not await _establish_position(signal, quantity, trade_result):
            return

    save(signal, order, trade_result)


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
        return True

    return False


async def _entry_passes_risk_gates(signal) -> bool:
    """Exposure, symbol-concentration and sector-concentration limits."""
    if not risk_engine.check_exposure():
        reason = risk_engine.exposure_block_reason()
        logger.warning(f"Risk limit reached, skipping {signal.symbol}: {reason}")
        await notifier.notify_risk_limit_hit(reason)
        return False

    if not risk_engine.can_trade_symbol(signal.symbol):
        logger.warning(f"Symbol concentration limit reached for {signal.symbol}")
        return False

    if not risk_engine.can_trade_sector(signal.symbol):
        logger.warning(f"Sector concentration limit reached for {signal.symbol}")
        return False

    return True


async def _resolve_entry_capital(signal) -> float | None:
    """Fetch live capital from OpenAlgo. Returns None if the entry cannot be funded."""
    capital = await fetch_available_capital()
    if capital <= 0:
        logger.error("Cannot fetch capital from OpenAlgo, skipping trade")
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
        return None

    return capital


async def _resolve_entry_quantity(
    signal, capital: float, sizing_capital: float
) -> "tuple[int, bool] | None":
    """Size the position and fit it to broker margin.

    Returns (quantity, is_analyze) or None if the trade cannot be taken.
    """
    quantity = risk_engine.calculate_quantity(signal, capital=sizing_capital)
    if quantity <= 0:
        msg = f"Sizing returned 0 for {signal.symbol} — entry price too high for risk budget ({signal.entry:.2f} vs capital={sizing_capital:,.0f})"
        logger.info(msg)
        await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
        return None

    # Adjust qty to fit actual broker margin (uses live capital, not day-start)
    # Skip in analyze mode: sandbox has fixed virtual capital, broker margin API is not available
    _, is_analyze = await fetch_trading_mode()
    if is_analyze:
        logger.info(f"Analyze mode: skipping margin check for {signal.symbol}, using risk-based qty={quantity}")
    else:
        try:
            quantity = await adjust_qty_for_margin(signal, quantity, capital)
        except MarginAPIError as e:
            msg = f"Margin API failed: {e}"
            logger.error(f"{signal.symbol}: {msg}, skipping trade")
            await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
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

    return quantity, is_analyze


def _log_entry_sizing(signal, quantity: int, sizing_capital: float) -> float:
    """Emit the one-line sizing audit trail. Returns the reward:risk ratio."""
    risk_per_share = abs(signal.entry - signal.sl)
    risk_amount = sizing_capital * settings.risk_per_trade
    # Effective SL used for sizing — may be capped if max_sl_pct_for_sizing is set
    effective_sl = risk_per_share
    if settings.max_sl_pct_for_sizing > 0 and signal.entry > 0:
        max_sl_dist = signal.entry * settings.max_sl_pct_for_sizing
        if risk_per_share > max_sl_dist:
            effective_sl = max_sl_dist
    adjusted_rps = effective_sl * (1 + settings.slippage_factor)
    reward_per_share = abs(signal.tp - signal.entry)
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0
    pos_value = quantity * signal.entry
    risk_total = quantity * risk_per_share
    cap_note = (
        f" [SL capped {risk_per_share:.2f}->{effective_sl:.2f} for sizing]"
        if effective_sl < risk_per_share else ""
    )
    logger.info(
        f"Sizing [{signal.symbol}]: capital={sizing_capital:,.0f} risk={settings.risk_per_trade:.1%}={risk_amount:,.0f} "
        f"entry={signal.entry} sl={signal.sl} tp={signal.tp} "
        f"risk/sh={risk_per_share:.2f}(+{settings.slippage_factor:.0%}slip={adjusted_rps:.2f}){cap_note} "
        f"reward/sh={reward_per_share:.2f} R:R=1:{rr:.1f} "
        f"qty=floor({risk_amount:,.0f}/{adjusted_rps:.2f})={quantity} "
        f"value={pos_value:,.0f} total_risk={risk_total:,.0f}({risk_total/sizing_capital:.2%})"
    )
    return rr


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


async def _notify_entry_outcome(signal, trade_result, rr: float) -> None:
    """Announce the broker's response to the entry order."""
    if trade_result.status == OrderStatus.SUCCESS:
        logger.info(f"Order placed for {signal.symbol}: id={trade_result.order_id}")
        # +1 because risk_engine.record_trade runs below — this slot is now taken.
        slot_context = notifier.format_slot_context(
            risk_engine.open_positions + 1, risk_engine.max_open_positions
        )
        await notifier.notify_order_placed(
            signal.symbol, signal.direction.value,
            strategy=signal.strategy, signal_price=signal.entry,
            sl=signal.sl, tp=signal.tp, rr=rr,
            slot_context=slot_context,
        )
    else:
        logger.warning(f"Order {trade_result.status.value} for {signal.symbol}: {trade_result.message}")
        await notifier.notify_order_rejected(signal.symbol, trade_result.message, strategy=signal.strategy)


async def _establish_position(signal, quantity: int, trade_result) -> bool:
    """Record the trade, protect it with an SL, and register it for tracking.

    Returns False when the fill overshot TP and the position was auto-closed — the
    caller must then skip the DB save.
    """
    risk_engine.record_trade(symbol=signal.symbol)
    logger.info(f"Capacity: {risk_engine.capacity_status()}")

    sl_order_id = await _place_entry_bracket(signal, quantity, trade_result.order_id)
    entry_fill_price = await _fetch_entry_fill(signal, trade_result.order_id)

    if await _auto_close_on_tp_overshoot(signal, quantity, entry_fill_price, sl_order_id):
        return False

    # Always notify LIVE so trader knows position is active and SL is protecting it.
    # fill_price=0 triggers "fill pending" wording in the message.
    await notifier.notify_entry_filled(
        signal.symbol, signal.direction.value, entry_fill_price or 0.0,
        quantity, signal.entry, strategy=signal.strategy, sl=signal.sl, tp=signal.tp,
    )

    tracker.register(TrackedPosition(
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
    ))
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
        await notifier.notify_sl_placed(signal.symbol, sl_order_id, strategy=signal.strategy, sl_price=signal.sl)
    else:
        await notifier.notify_sl_failed(signal.symbol, sl_result.message if sl_result else "no result", strategy=signal.strategy)
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


async def _auto_close_on_tp_overshoot(
    signal, quantity: int, entry_fill_price, sl_order_id: str
) -> bool:
    """Close immediately if the fill landed past TP. True if the position was closed.

    High slippage can push the fill past the TP target, leaving the position with
    negative reward and a disproportionately wide SL.
    """
    fill_overshot_tp = entry_fill_price and (
        (signal.direction == Direction.LONG  and entry_fill_price >= signal.tp) or
        (signal.direction == Direction.SHORT and entry_fill_price <= signal.tp)
    )
    if not fill_overshot_tp:
        return False

    logger.error(
        f"Fill overshot TP for {signal.symbol}: fill={entry_fill_price:.2f} "
        f"tp={signal.tp:.2f} direction={signal.direction.value} — auto-closing position"
    )
    if sl_order_id:
        await cancel_order(sl_order_id, signal.strategy)
    close_order = build_exit_order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        quantity=quantity,
        product=signal.product or settings.product,
        strategy_tag=signal.strategy,
        direction=signal.direction,
    )
    await send_order(close_order)
    risk_engine.record_close(0.0, symbol=signal.symbol)
    await notifier.notify_order_rejected(
        signal.symbol,
        f"fill {entry_fill_price:.2f} overshot TP {signal.tp:.2f} — auto-closed",
        strategy=signal.strategy,
    )
    return True


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

    logger.bind(symbol=signal.symbol).info(
        f"Parsed signal: {signal.strategy} {signal.direction.value} {signal.symbol} "
        f"entry={signal.entry} sl={signal.sl} tp={signal.tp}"
    )

    # 2. Validate
    result = validate(signal)
    if result.status != ValidationStatus.VALID:
        logger.info(f"Signal {result.status.value}: {result.reason}")
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
    startup.start_engine(risk_engine, tracker, handle_message)


if __name__ == "__main__":
    main()
