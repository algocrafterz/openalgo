"""Entry point and pipeline orchestration for the signal engine."""

import asyncio
import math
import signal
import sys

from loguru import logger

from signal_engine.api_client import cancel_order, fetch_available_capital, fetch_open_position, fetch_order_fill_price, fetch_realised_pnl, fetch_trading_mode, fetch_margin, MarginAPIError
from signal_engine.config import settings
from signal_engine.db import save
from signal_engine.executor import build_exit_order, build_order, place_sl_order, send_bracket_legs, send_order
from signal_engine.listener import start_listener
from signal_engine.logger_setup import setup_logger
from signal_engine.models import Direction, OrderStatus, TradeResult, ValidationStatus
from signal_engine import notifier
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.risk import RiskEngine
from signal_engine.risk_store import RiskStore, RISK_DB_PATH
from signal_engine.tracker import PositionTracker, TimeExitScheduler, TrackedPosition
from signal_engine.validator import validate

# Persistent risk counter store (keyed by mode + date, survives restarts)
_risk_store = RiskStore(RISK_DB_PATH)

# Global risk engine instance (counters restored from store on startup)
risk_engine = RiskEngine(
    risk_per_trade=settings.risk_per_trade,
    sizing_mode=settings.sizing_mode,
    pct_of_capital=settings.pct_of_capital,
    daily_loss_limit=settings.daily_loss_limit,
    weekly_loss_limit=settings.weekly_loss_limit,
    monthly_loss_limit=settings.monthly_loss_limit,
    max_open_positions=settings.max_open_positions,
    max_trades_per_day=settings.max_trades_per_day,
    min_entry_price=settings.min_entry_price,
    max_entry_price=settings.max_entry_price,
    slippage_factor=settings.slippage_factor,
    store=_risk_store,
    trade_mode="live",
    default_product=settings.product,
    max_positions_per_symbol=settings.max_positions_per_symbol,
    max_positions_per_sector=settings.max_positions_per_sector,
    sectors=settings.sectors,
    use_day_start_capital=settings.use_day_start_capital,
)

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
        # SpanCalc only supports derivatives — estimate equity MIS margin instead.
        # Binary reject: if full-risk qty doesn't fit in live capital, skip the trade.
        # We never scale qty down — a scaled trade risks less than 1% and produces
        # dwarf positions that are not worth the commission + slippage cost.
        # Sizing uses day-start capital (equal risk weighting); this check uses live_capital
        # as a hard floor to prevent broker rejections.
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
    """Inner exit handler — must be called with the per-position exit lock held."""
    # 0. Abort if time_exit_all() is already closing all positions to avoid double-exit.
    # time_exit cancels all orders then fires close_all_positions — a concurrent TP signal
    # would try to cancel the same SL (already gone) and place a duplicate exit order.
    if tracker._time_exit_active:
        logger.info(
            f"EXIT skipped for {signal.symbol}: time_exit_all() in progress, positions closing via time exit"
        )
        return

    # 1. Look up position in tracker
    pos = tracker.find_position(signal.symbol, signal.strategy)

    # Guard against duplicate concurrent processing. asyncio.Lock serializes tasks, but
    # Telethon can dispatch multiple simultaneous TP alerts as separate tasks before any
    # acquires the lock. The flag is set/cleared synchronously (no await between check
    # and set), so it is safe in single-threaded asyncio — it catches the rare case where
    # the lock re-enters before the previous handler fully clears the position.
    if pos is not None and getattr(pos, "exit_pending", False) is True:
        logger.warning(
            f"EXIT: {signal.symbol} exit already in progress (duplicate TP signal), skipping"
        )
        return
    if pos is not None:
        pos.exit_pending = True

    # 2. Fallback: if tracker lost state (engine restart), query broker API
    if pos is None:
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
            return
        # Negative qty from broker means SHORT position; positive means LONG.
        # Use the absolute value for quantity and set direction accordingly.
        fallback_direction = Direction.SHORT if api_qty < 0 else Direction.LONG
        pos = TrackedPosition(
            symbol=signal.symbol,
            strategy=signal.strategy,
            exchange=exchange,
            product=product,
            entry_price=signal.entry,
            quantity=abs(api_qty),
            sl=signal.sl,
            tp=signal.tp,
            direction=fallback_direction,
            sl_order_id="",
        )

    # 3. Resolve exit quantity (partial or full based on tp_level config)
    exit_qty, is_full_exit = _resolve_exit_qty(signal, pos)
    tp_level = getattr(signal, "tp_level", None)
    logger.info(
        f"EXIT: {pos.symbol} tp_level={tp_level} exit_qty={exit_qty}/{pos.quantity} "
        f"full_exit={is_full_exit}"
    )

    # 4. ALWAYS cancel SL before placing exit order.
    # Indian brokers treat any SELL while SL SELL is active as a new SHORT position
    # (FUND LIMIT INSUFFICIENT). SL must be cancelled first, even for partial exits.
    if pos.sl_order_id:
        success = await cancel_order(pos.sl_order_id, pos.strategy)
        if success:
            logger.info(f"EXIT: SL order {pos.sl_order_id} cancelled for {pos.symbol}")
        else:
            logger.warning(f"EXIT: failed to cancel SL {pos.sl_order_id} for {pos.symbol}")

    # 5. Build and send MARKET SELL exit order — retry up to tp_exit_retries on failure.
    # MARKET orders rarely reject but network timeouts are possible. We retry on any
    # non-SUCCESS status. After SL cancel the position is unprotected, so we must exit.
    exit_order = build_exit_order(
        symbol=pos.symbol,
        exchange=pos.exchange,
        quantity=exit_qty,
        product=pos.product,
        strategy_tag=pos.strategy,
        direction=pos.direction,
    )
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

    if trade_result.status == OrderStatus.SUCCESS:
        logger.info(f"EXIT order placed for {pos.symbol}: id={trade_result.order_id} qty={exit_qty}")

        # 6. Compute PnL from realised PnL delta (same method as check_positions).
        # Lock prevents race with check_positions polling concurrently at the same time.
        async with tracker._pnl_lock:
            current_realised = await fetch_realised_pnl()
            pnl_delta = current_realised - tracker._last_realised_pnl
            tracker._last_realised_pnl = current_realised
        logger.info(f"EXIT PnL for {pos.symbol}: delta={pnl_delta:,.2f} (realised={current_realised:,.2f})")

        if is_full_exit:
            # Full exit: unregister position, free risk slot, notify with PnL
            await notifier.notify_position_closed(pos.symbol, pnl_delta, strategy=pos.strategy)
            tracker.unregister(signal.symbol, signal.strategy)
            risk_engine.record_close(pnl=pnl_delta, symbol=pos.symbol)
            # exit_pending does not need clearing — position is unregistered
        else:
            # Partial exit: reduce tracked qty, keep position registered
            remaining = pos.quantity - exit_qty
            if remaining <= 0:
                # Defensive: arithmetic produced invalid remainder — treat as full exit
                logger.warning(
                    f"Partial exit produced invalid remainder {remaining} for {pos.symbol} "
                    f"(qty={pos.quantity}, exit_qty={exit_qty}), converting to full exit"
                )
                tracker.unregister(signal.symbol, signal.strategy)
                risk_engine.record_close(pnl=pnl_delta, symbol=pos.symbol)
                await notifier.notify_position_closed(pos.symbol, pnl_delta, strategy=pos.strategy)
                tracker.record_exit(pnl=pnl_delta, new_realised_pnl=current_realised)
                if not tracker._positions:
                    await tracker.send_day_summary()
                return
            pos.quantity = remaining
            pos.sl_order_id = ""  # clear old SL id — will be updated below if re-placement succeeds
            logger.info(f"Partial exit: {pos.symbol} exited {exit_qty}, remaining {remaining}")

            # Re-place SL at TP1 - 0.1R after partial TP exit (50-50 TP booking strategy).
            # Rationale: TP1 is freshly-hit resistance. Placing SL exactly at TP1 means any
            # normal wick-back immediately stops the runner. Buffer = 0.1R gives the runner
            # room to breathe without giving back significant profit (worst case: runner exits
            # at TP1 - 0.1R = still +0.9R on that half, vs -1R original SL).
            # Buffer is direction-aware: LONG = TP1 - buffer, SHORT = TP1 + buffer.
            # Fall back to entry_price if pos.tp is unset — defensive, shouldn't occur.
            # Critical: SL was already cancelled before the exit order. Re-place now.
            if pos.tp and pos.tp > 0:
                risk_distance = abs(pos.tp - pos.entry_price)
                buffer = settings.tp1_runner_sl_buffer * risk_distance
                if pos.direction == Direction.LONG:
                    new_sl_price = pos.tp - buffer
                else:
                    new_sl_price = pos.tp + buffer
            else:
                new_sl_price = pos.entry_price
            if settings.bracket_enabled and new_sl_price > 0:
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

            await notifier.notify_partial_exit(
                pos.symbol, exit_qty, remaining, tp_level or "", pnl_delta,
                strategy=pos.strategy, new_sl=pos.sl,
            )
            pos.exit_pending = False  # partial exit done — allow next TP signal

        # 7. Update day summary counters + realised PnL snapshot
        tracker.record_exit(pnl=pnl_delta, new_realised_pnl=current_realised)

        # 8. Send day summary if this was the last open position
        if is_full_exit and not tracker._positions:
            await tracker.send_day_summary()
    else:
        logger.error(f"EXIT order failed for {pos.symbol}: {trade_result.message}")
        # Check if broker position is already 0 — SL may have fired in the window between
        # our SL cancel attempt and this exit order (race condition on fast reversals).
        # If already flat, clean up tracker silently instead of alarming the user.
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

    # 8. Persist to DB audit trail
    save(signal, exit_order, trade_result)


async def _handle_entry(signal) -> None:
    """Handle a LONG/SHORT entry signal — the existing ORB pipeline.

    Pipeline: check_exposure -> size -> build_order -> send -> bracket -> track -> save
    """
    # 3. Check exposure limits
    if not risk_engine.check_exposure():
        reason = risk_engine.exposure_block_reason()
        logger.warning(f"Risk limit reached, skipping {signal.symbol}: {reason}")
        await notifier.notify_risk_limit_hit(reason)
        return

    # 3a. Symbol concentration check
    if not risk_engine.can_trade_symbol(signal.symbol):
        logger.warning(f"Symbol concentration limit reached for {signal.symbol}")
        return

    # 3b. Sector concentration check
    if not risk_engine.can_trade_sector(signal.symbol):
        logger.warning(f"Sector concentration limit reached for {signal.symbol}")
        return

    # 4. Fetch capital from OpenAlgo funds API
    #    Returns live broker capital or sandbox capital depending on mode
    capital = await fetch_available_capital()
    if capital <= 0:
        logger.error("Cannot fetch capital from OpenAlgo, skipping trade")
        return

    # 4a. Minimum capital floor — skip entry if live capital is too depleted.
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
        return

    # Use day-start capital for equal risk per trade (cached on first fetch of day)
    sizing_capital = risk_engine.get_sizing_capital(capital)
    logger.info(f"Capital: live={capital:,.2f} sizing={sizing_capital:,.2f} INR")

    # 5. Calculate position size with sizing capital (day-start if enabled)
    quantity = risk_engine.calculate_quantity(signal, capital=sizing_capital)
    if quantity <= 0:
        msg = f"Sizing returned 0 for {signal.symbol} — entry price too high for risk budget ({signal.entry:.2f} vs capital={sizing_capital:,.0f})"
        logger.info(msg)
        await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
        return

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
            return
        if quantity <= 0:
            msg = f"Insufficient capital after margin check — {signal.symbol} requires more margin than available (capital={capital:,.0f})"
            logger.warning(msg)
            await notifier.notify_order_rejected(signal.symbol, msg, strategy=signal.strategy)
            return

    # Apply test qty cap if configured (for minimal exposure live testing)
    if settings.test_qty_cap > 0 and quantity > settings.test_qty_cap:
        logger.info(f"Test qty cap: {quantity} -> {settings.test_qty_cap} for {signal.symbol}")
        quantity = settings.test_qty_cap

    risk_per_share = abs(signal.entry - signal.sl)
    risk_amount = sizing_capital * settings.risk_per_trade
    reward_per_share = abs(signal.tp - signal.entry)
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0
    pos_value = quantity * signal.entry
    risk_total = quantity * risk_per_share
    logger.info(
        f"Sizing [{signal.symbol}]: capital={sizing_capital:,.0f} risk={settings.risk_per_trade:.1%}={risk_amount:,.0f} "
        f"entry={signal.entry} sl={signal.sl} tp={signal.tp} "
        f"risk/sh={risk_per_share:.2f} reward/sh={reward_per_share:.2f} R:R=1:{rr:.1f} "
        f"qty=floor({risk_amount:,.0f}/{risk_per_share:.2f})={quantity} "
        f"value={pos_value:,.0f} total_risk={risk_total:,.0f}({risk_total/sizing_capital:.2%})"
    )

    # 6. Build order
    # In analyze mode with off-hours testing enabled, override MIS→CNC to bypass sandbox
    # after-hours restriction (sandbox blocks new MIS orders outside 09:00–squareoff window)
    off_hours_product_override = (
        is_analyze
        and settings.allow_off_hours_testing
        and (signal.product or settings.product) == "MIS"
    )
    if off_hours_product_override:
        logger.info(f"Off-hours testing: overriding product MIS→CNC for {signal.symbol}")
    order = build_order(signal, quantity, product="CNC" if off_hours_product_override else "")

    # 7. Send to OpenAlgo (routes to live broker or sandbox automatically)
    trade_result = await send_order(order)

    if trade_result.status == OrderStatus.SUCCESS:
        logger.info(f"Order placed for {signal.symbol}: id={trade_result.order_id}")
        await notifier.notify_order_placed(signal.symbol, signal.direction.value, trade_result.order_id, strategy=signal.strategy)
    else:
        logger.warning(f"Order {trade_result.status.value} for {signal.symbol}: {trade_result.message}")
        await notifier.notify_order_rejected(signal.symbol, trade_result.message, strategy=signal.strategy)

    if trade_result.status == OrderStatus.SUCCESS:
        # 8. Record trade in risk engine
        risk_engine.record_trade(symbol=signal.symbol)
        logger.info(f"Capacity: {risk_engine.capacity_status()}")

        # 9. Place SL bracket leg (MIS only)
        # TP is NOT placed as a broker order — Indian brokers treat a second SELL as a new
        # short, causing FUND LIMIT INSUFFICIENT. TP exit is driven by TradingView TP HIT
        # signal -> _handle_exit pipeline. SL-M placed here as broker-side safety net.
        # CNC: SL-M cancelled at EOD by NSE, no GTT in OpenAlgo — skip bracket entirely.
        # CNC exits rely on TradingView EXIT alerts (close < 200 SMA, max hold days).
        sl_order_id = ""
        product = signal.product or settings.product
        skip_cnc_bracket = product == "CNC" and not settings.bracket_cnc_sl_enabled
        if skip_cnc_bracket:
            logger.info(
                f"Skipping SL bracket for {signal.symbol}: CNC product, "
                "SL-M cancelled at EOD by NSE (bracket.cnc_sl_enabled=false)"
            )
        if settings.bracket_enabled and not skip_cnc_bracket:
            sl_result, _ = await send_bracket_legs(signal, quantity, trade_result.order_id)
            sl_order_id = sl_result.order_id if sl_result else ""
            if sl_result and sl_result.status == OrderStatus.SUCCESS:
                await notifier.notify_sl_placed(signal.symbol, signal.direction.value, sl_order_id, strategy=signal.strategy)
            else:
                await notifier.notify_sl_failed(signal.symbol, sl_result.message if sl_result else "no result", strategy=signal.strategy)

        # 10. Fetch actual entry fill price (MARKET orders typically fill before bracket completes)
        entry_fill_price = await fetch_order_fill_price(trade_result.order_id, signal.strategy)
        if entry_fill_price:
            slippage = entry_fill_price - signal.entry
            logger.info(
                f"Entry fill: {signal.symbol} avg_price={entry_fill_price:.2f} "
                f"(signal={signal.entry:.2f}, slippage={slippage:+.2f})"
            )
            await notifier.notify_entry_filled(
                signal.symbol, signal.direction.value, entry_fill_price,
                quantity, signal.entry, strategy=signal.strategy,
            )
        else:
            logger.warning(f"Entry fill price unavailable for {signal.symbol} id={trade_result.order_id}")

        # 11. Register in position tracker for P&L monitoring and SL close-detection
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

    # 12. Persist to DB
    save(signal, order, trade_result)


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

    logger.info(
        f"Parsed signal: {signal.strategy} {signal.direction.value} {signal.symbol} "
        f"entry={signal.entry} sl={signal.sl} tp={signal.tp}"
    )

    # 2. Validate
    result = validate(signal)
    if result.status != ValidationStatus.VALID:
        logger.info(f"Signal {result.status.value}: {result.reason}")
        return

    # 3. Dispatch based on direction
    if signal.direction == Direction.EXIT:
        await _handle_exit(signal)
    else:
        await _handle_entry(signal)


async def _test_signal(text: str) -> None:
    """Process a single test signal through the full pipeline, then exit.

    Used with --test CLI flag for manual live market testing without Telegram.
    """
    setup_logger()
    logger.info("Signal Engine TEST MODE — processing single signal")

    if settings.test_qty_cap > 0:
        logger.info(f"Test qty cap active: max {settings.test_qty_cap} shares per order")
    else:
        logger.warning("test_qty_cap is 0 (disabled) — full position sizing will be used")

    # Detect trading mode for logging
    mode_str, is_analyze = await fetch_trading_mode()
    mode_label = "ANALYZE" if is_analyze else "LIVE"
    logger.info(f"Trading mode: {mode_label}")

    capital = await fetch_available_capital()
    if capital > 0:
        logger.info(f"Available capital: {capital:,.2f} INR")

    logger.info(f"Input signal:\n{text}")
    await handle_message(text)
    logger.info("Test signal processing complete")


def main() -> None:
    """Entry point — start the signal engine with position tracker."""
    setup_logger()

    # --smoke-test: connectivity + pipeline health check, exit with 0/1
    if "--smoke-test" in sys.argv or "--dry-run" in sys.argv:
        from signal_engine.smoke_test import run_smoke_test, run_dry_run
        is_dry = "--dry-run" in sys.argv

        async def _run_checks():
            report = await run_dry_run() if is_dry else await run_smoke_test()
            report.print()
            sys.exit(0 if report.all_passed else 1)

        try:
            asyncio.run(_run_checks())
        except KeyboardInterrupt:
            sys.exit(1)
        return

    # --test mode: read signal from stdin or file, process once, exit
    if "--test" in sys.argv:
        # Read signal text from: --test "signal text" OR --test-file path OR stdin
        idx = sys.argv.index("--test")
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
            signal_text = sys.argv[idx + 1]
        elif "--test-file" in sys.argv:
            fidx = sys.argv.index("--test-file")
            if fidx + 1 < len(sys.argv):
                with open(sys.argv[fidx + 1]) as f:
                    signal_text = f.read().strip()
            else:
                logger.error("--test-file requires a path argument")
                sys.exit(1)
        else:
            logger.info("Reading signal from stdin (paste signal, then Ctrl+D):")
            signal_text = sys.stdin.read().strip()

        if not signal_text:
            logger.error("No signal text provided")
            sys.exit(1)

        asyncio.run(_test_signal(signal_text))
        sys.exit(0)

    logger.info("Signal Engine starting")
    logger.info(f"Sizing mode: {settings.sizing_mode}")
    if settings.use_day_start_capital:
        logger.info("Day-start capital: enabled (equal risk per trade)")
    logger.info(f"Min R:R: {settings.min_rr}")
    logger.info(f"Position poll interval: {settings.poll_interval}s")
    for ch in settings.telegram_channels:
        logger.info(f"Telegram channel: {ch.name} ({ch.id})")

    async def run():
        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        def _signal_handler():
            logger.info("Shutdown signal received, stopping gracefully...")
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        # ── Startup health checks ──────────────────────────────────────────────
        # Run before accepting any signals. Critical failures abort startup and
        # notify via Telegram. Warning failures are logged but allow startup.
        from signal_engine.smoke_test import run_startup_checks, _CRITICAL_CHECKS, _WARNING_CHECKS
        startup_report = await run_startup_checks()
        startup_report.print()

        failed_critical = [c for c in startup_report.checks if not c.passed and c.name in _CRITICAL_CHECKS]
        failed_warnings = [c for c in startup_report.checks if not c.passed and c.name in _WARNING_CHECKS]

        if failed_warnings:
            warn_lines = "\n".join(f"  WARN {c.name}: {c.message}" for c in failed_warnings)
            logger.warning(f"Startup warnings (non-fatal):\n{warn_lines}")

        if failed_critical:
            fail_lines = "\n".join(f"  FAIL {c.name}: {c.message}" for c in failed_critical)
            logger.critical(f"Startup checks FAILED — aborting:\n{fail_lines}")
            summary = "\n".join(f"FAIL: {c.name}\n  {c.message}" for c in failed_critical)
            await notifier.notify_startup_result(all_passed=False, summary=summary)
            shutdown_event.set()
            return

        # Notify startup result (pass summary with warnings if any)
        warn_note = ""
        if failed_warnings:
            warn_note = "\nWarnings:\n" + "\n".join(f"  {c.name}" for c in failed_warnings)
        await notifier.notify_startup_result(all_passed=True, summary=f"All critical checks passed.{warn_note}")
        # ── End startup checks ─────────────────────────────────────────────────

        # Detect and log trading mode
        mode_str, is_analyze = await fetch_trading_mode()
        if is_analyze:
            logger.info("Trading mode: ANALYZE (sandbox capital)")
        else:
            logger.info("Trading mode: LIVE (broker capital)")

        # ── Position reconciliation ────────────────────────────────────────────
        # Reconcile stored open_positions against actual broker positions on startup.
        # Prevents stale counter from blocking new trades if engine was stopped while
        # positions were open (broker auto-squareoff, SL hit during downtime, etc.).
        if risk_engine.open_positions > 0:
            from signal_engine.api_client import fetch_positionbook
            positions = await fetch_positionbook()
            if positions is not None:
                configured_product = settings.product  # MIS or CNC
                product_map = {"MIS": "I", "CNC": "C", "NRML": "M"}
                broker_product = product_map.get(configured_product, configured_product)
                actual_open = sum(
                    1 for p in positions
                    if int(p.get("quantity", 0)) != 0
                    and p.get("product", "").upper() in (configured_product, broker_product)
                )
                if actual_open != risk_engine.open_positions:
                    logger.warning(
                        f"Position mismatch: stored open_positions={risk_engine.open_positions}, "
                        f"broker reports {actual_open} open — correcting and persisting"
                    )
                    # Correct heat proportionally if positions differ
                    if actual_open == 0:
                        risk_engine.portfolio_heat = 0.0
                    elif risk_engine.open_positions > 0:
                        risk_engine.portfolio_heat = (
                            risk_engine.portfolio_heat * actual_open / risk_engine.open_positions
                        )
                    risk_engine.open_positions = actual_open
                    risk_engine._persist()
                else:
                    logger.info(f"Position reconciliation: stored={risk_engine.open_positions} matches broker={actual_open}")
            else:
                logger.warning("Position reconciliation: could not fetch positionbook, skipping")
        # ── End position reconciliation ────────────────────────────────────────

        # Log risk state summary (restored counters + config)
        startup_capital = await fetch_available_capital()
        if startup_capital > 0:
            risk_engine.log_startup_summary(startup_capital)
            mode_label = "ANALYZE" if is_analyze else "LIVE"
            await notifier.notify_engine_started(startup_capital, mode_label)

        # Start position tracker in background
        tracker_task = asyncio.create_task(tracker.start())

        # Start time exit scheduler if enabled
        time_exit_task = None
        if settings.time_exit_enabled:
            time_exit_scheduler = TimeExitScheduler(
                tracker, settings.time_exit_hour, settings.time_exit_minute
            )
            time_exit_task = asyncio.create_task(time_exit_scheduler.start())
            logger.info(
                f"Time exit enabled: {settings.time_exit_hour:02d}:{settings.time_exit_minute:02d} IST"
            )

        try:
            listener_task = asyncio.create_task(start_listener(handle_message))
            # Wait for either the listener to finish or a shutdown signal
            _, pending = await asyncio.wait(
                [listener_task, asyncio.create_task(shutdown_event.wait())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Send stopped notification while the Telethon client is still alive
            # (listener_task not yet cancelled — must happen before task.cancel())
            try:
                await asyncio.wait_for(notifier.notify_engine_stopped(), timeout=5.0)
            except Exception:
                logger.debug("Could not send engine stopped notification")
            for task in pending:
                task.cancel()
        finally:
            tracker.stop()
            tracker_task.cancel()
            if time_exit_task is not None:
                time_exit_scheduler.stop()
                time_exit_task.cancel()
            logger.info("Signal Engine stopped")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Signal Engine interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
