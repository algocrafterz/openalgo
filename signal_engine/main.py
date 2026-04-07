"""Entry point and pipeline orchestration for the signal engine."""

import asyncio
import math
import signal
import sys

from loguru import logger

from signal_engine.api_client import cancel_order, fetch_available_capital, fetch_open_position, fetch_realised_pnl, fetch_trading_mode, fetch_margin, MarginAPIError
from signal_engine.config import settings
from signal_engine.db import save
from signal_engine.executor import build_exit_order, build_order, place_sl_order, send_bracket_legs, send_order
from signal_engine.listener import start_listener
from signal_engine.logger_setup import setup_logger
from signal_engine.models import Direction, OrderStatus, ValidationStatus
from signal_engine import notifier
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.risk import RiskEngine
from signal_engine.risk_store import RiskStore
from signal_engine.tracker import PositionTracker, TimeExitScheduler, TrackedPosition
from signal_engine.validator import validate

# Persistent risk counter store (keyed by mode + date, survives restarts)
_risk_store = RiskStore("db/risk.db")

# Global risk engine instance (counters restored from store on startup)
risk_engine = RiskEngine(
    risk_per_trade=settings.risk_per_trade,
    sizing_mode=settings.sizing_mode,
    pct_of_capital=settings.pct_of_capital,
    daily_loss_limit=settings.daily_loss_limit,
    weekly_loss_limit=settings.weekly_loss_limit,
    monthly_loss_limit=settings.monthly_loss_limit,
    max_portfolio_heat=settings.max_portfolio_heat,
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


async def adjust_qty_for_margin(signal, raw_qty: int, live_capital: float) -> int:
    """Adjust risk-based qty to fit within available broker margin.

    Queries the Margin API for the actual margin required for raw_qty.
    If it exceeds live_capital, scales qty down proportionally (MIS margin is linear with qty).
    Raises MarginAPIError on persistent API failure — trade is skipped.

    Returns adjusted qty, or 0 if it cannot fit in available capital.
    """
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

    Uses strategy_profiles tp_levels config for partial exits.
    Returns (exit_qty, is_full_exit).
    """
    tp_level = getattr(signal, "tp_level", None)
    profile = settings.strategy_profiles.get(signal.strategy.upper(), {})
    tp_levels = profile.get("tp_levels", {})

    # No tp_level (safety EXIT) or no profile -> full exit
    if not tp_level or not tp_levels:
        return pos.quantity, True

    exit_pct = tp_levels.get(tp_level.upper(), 1.0)

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

    # 1. Look up position in tracker
    pos = tracker.find_position(signal.symbol, signal.strategy)

    # 2. Fallback: if tracker lost state (engine restart), query broker API
    if pos is None:
        logger.warning(
            f"EXIT: {signal.symbol} not in tracker for strategy={signal.strategy}, "
            "checking broker API (engine restart fallback)"
        )
        exchange = signal.exchange or settings.exchange
        product = signal.product or settings.product
        api_qty = await fetch_open_position(signal.symbol, signal.strategy, exchange, product)
        if api_qty <= 0:
            logger.warning(f"EXIT: no open position for {signal.symbol} (strategy={signal.strategy})")
            await notifier.notify_exit_no_position(signal.symbol, signal.strategy)
            return
        pos = TrackedPosition(
            symbol=signal.symbol,
            strategy=signal.strategy,
            exchange=exchange,
            product=product,
            entry_price=signal.entry,
            quantity=api_qty,
            sl=signal.sl,
            tp=signal.tp,
            direction=Direction.LONG,
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

    # 5. Build and send MARKET SELL exit order
    exit_order = build_exit_order(
        symbol=pos.symbol,
        exchange=pos.exchange,
        quantity=exit_qty,
        product=pos.product,
        strategy_tag=pos.strategy,
    )
    trade_result = await send_order(exit_order)

    if trade_result.status == OrderStatus.SUCCESS:
        logger.info(f"EXIT order placed for {pos.symbol}: id={trade_result.order_id} qty={exit_qty}")

        # 6. Compute PnL from realised PnL delta (same method as check_positions)
        current_realised = await fetch_realised_pnl()
        pnl_delta = current_realised - tracker._last_realised_pnl
        logger.info(f"EXIT PnL for {pos.symbol}: delta={pnl_delta:,.2f} (realised={current_realised:,.2f})")

        if is_full_exit:
            # Full exit: unregister position, free risk slot
            await notifier.notify_exit_placed(pos.symbol, trade_result.order_id, strategy=pos.strategy)
            tracker.unregister(signal.symbol, signal.strategy)
            risk_engine.record_close(pnl=pnl_delta, symbol=pos.symbol)
        else:
            # Partial exit: reduce tracked qty, keep position registered
            remaining = pos.quantity - exit_qty
            pos.quantity = remaining
            pos.sl_order_id = ""  # clear old SL id — will be updated below if re-placement succeeds
            logger.info(f"Partial exit: {pos.symbol} exited {exit_qty}, remaining {remaining}")

            # Re-place SL for remaining qty (original SL price — no trailing).
            # Critical: SL was already cancelled before the exit order. Re-place now so the
            # remaining position is protected. Any partial exit -> new SL for remaining qty.
            if settings.bracket_enabled and pos.sl > 0:
                sl_result = await place_sl_order(
                    symbol=pos.symbol,
                    exchange=pos.exchange,
                    direction=pos.direction,
                    quantity=remaining,
                    sl_price=pos.sl,
                    product=pos.product,
                    strategy_tag=pos.strategy,
                )
                if sl_result.status == OrderStatus.SUCCESS:
                    pos.sl_order_id = sl_result.order_id
                    logger.info(f"SL re-placed for {pos.symbol} remaining {remaining} qty: id={sl_result.order_id}")
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
                pos.symbol, exit_qty, remaining, tp_level or "", pnl_delta, strategy=pos.strategy,
            )

        # 7. Update day summary counters + realised PnL snapshot
        tracker.record_exit(pnl=pnl_delta, new_realised_pnl=current_realised)
    else:
        logger.error(f"EXIT order failed for {pos.symbol}: {trade_result.message}")
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

    # Use day-start capital for equal risk per trade (cached on first fetch of day)
    sizing_capital = risk_engine.get_sizing_capital(capital)
    logger.info(f"Capital: live={capital:,.2f} sizing={sizing_capital:,.2f} INR")

    # 5. Calculate position size with sizing capital (day-start if enabled)
    quantity = risk_engine.calculate_quantity(signal, capital=sizing_capital)
    if quantity <= 0:
        logger.info(f"Skipping {signal.symbol}: position sizing returned 0")
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
            logger.error(f"Margin API failed for {signal.symbol}, skipping trade: {e}")
            return
        if quantity <= 0:
            logger.warning(f"Skipping {signal.symbol}: insufficient capital for minimum position after margin check")
            return

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

        # 10. Register in position tracker for P&L monitoring and SL close-detection
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


def main() -> None:
    """Entry point — start the signal engine with position tracker."""
    setup_logger()
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

        # Detect and log trading mode
        mode_str, is_analyze = await fetch_trading_mode()
        if is_analyze:
            logger.info("Trading mode: ANALYZE (sandbox capital)")
        else:
            logger.info("Trading mode: LIVE (broker capital)")

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
            for task in pending:
                task.cancel()
        finally:
            tracker.stop()
            tracker_task.cancel()
            if time_exit_task is not None:
                time_exit_scheduler.stop()
                time_exit_task.cancel()
            await notifier.notify_engine_stopped()
            logger.info("Signal Engine stopped")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Signal Engine interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
