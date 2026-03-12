"""Entry point and pipeline orchestration for the signal engine."""

import asyncio
import signal
import sys

from loguru import logger

from signal_engine.api_client import fetch_available_capital, fetch_trading_mode
from signal_engine.config import settings
from signal_engine.db import save
from signal_engine.executor import build_order, send_bracket_legs, send_order
from signal_engine.listener import start_listener
from signal_engine.logger_setup import setup_logger
from signal_engine.models import OrderStatus, ValidationStatus
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.risk import RiskEngine
from signal_engine.risk_store import RiskStore
from signal_engine.tracker import PositionTracker, TrackedPosition
from signal_engine.validator import validate

# Persistent risk counter store (keyed by mode + date, survives restarts)
_risk_store = RiskStore("db/risk.db")

# Global risk engine instance (counters restored from store on startup)
risk_engine = RiskEngine(
    risk_per_trade=settings.risk_per_trade,
    sizing_mode=settings.sizing_mode,
    pct_of_capital=settings.pct_of_capital,
    max_position_size=settings.max_position_size,
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
    margin_multiplier=settings.margin_multiplier,
    max_capital_utilization=settings.max_capital_utilization,
    default_product=settings.product,
    max_positions_per_symbol=settings.max_positions_per_symbol,
    max_positions_per_sector=settings.max_positions_per_sector,
    sectors=settings.sectors,
)

# Global position tracker
tracker = PositionTracker(risk_engine, poll_interval=settings.poll_interval)


async def handle_message(text: str) -> None:
    """Full sequential pipeline for a single signal message.

    Pipeline: parse -> validate -> fetch_capital -> check_exposure -> size -> build_order -> send -> track -> save
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

    # 3. Check exposure limits
    if not risk_engine.check_exposure():
        logger.warning(f"Risk limit reached, skipping {signal.symbol}")
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

    logger.info(f"Available capital: {capital:,.2f} INR")

    # 5. Calculate position size with live capital
    quantity = risk_engine.calculate_quantity(signal, capital=capital)
    if quantity <= 0:
        logger.info(f"Skipping {signal.symbol}: position sizing returned 0")
        return

    risk_per_share = abs(signal.entry - signal.sl)
    risk_amount = capital * settings.risk_per_trade
    reward_per_share = abs(signal.tp - signal.entry)
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else 0
    pos_value = quantity * signal.entry
    risk_total = quantity * risk_per_share
    logger.info(
        f"Sizing [{signal.symbol}]: capital={capital:,.0f} risk={settings.risk_per_trade:.1%}={risk_amount:,.0f} "
        f"entry={signal.entry} sl={signal.sl} tp={signal.tp} "
        f"risk/sh={risk_per_share:.2f} reward/sh={reward_per_share:.2f} R:R=1:{rr:.1f} "
        f"qty=floor({risk_amount:,.0f}/{risk_per_share:.2f})={quantity} "
        f"value={pos_value:,.0f} total_risk={risk_total:,.0f}({risk_total/capital:.2%})"
    )

    # 6. Build order
    order = build_order(signal, quantity)

    # 7. Send to OpenAlgo (routes to live broker or sandbox automatically)
    trade_result = await send_order(order)

    if trade_result.status == OrderStatus.SUCCESS:
        logger.info(f"Order placed for {signal.symbol}: id={trade_result.order_id}")
    else:
        logger.warning(f"Order {trade_result.status.value} for {signal.symbol}: {trade_result.message}")

    if trade_result.status == OrderStatus.SUCCESS:
        # 8. Record trade in risk engine and commit margin
        risk_engine.record_trade(symbol=signal.symbol)
        product = signal.product if signal.product else settings.product
        risk_engine.add_margin(quantity, signal.entry, product)
        logger.info(f"Capacity: {risk_engine.capacity_status()}")

        # 9. Place bracket SL + TP legs (if enabled)
        sl_order_id = ""
        tp_order_id = ""
        if settings.bracket_enabled:
            sl_result, tp_result = await send_bracket_legs(signal, quantity, trade_result.order_id)
            sl_order_id = sl_result.order_id if sl_result else ""
            tp_order_id = tp_result.order_id if tp_result else ""

        # 10. Register in position tracker for P&L monitoring
        tracker.register(TrackedPosition(
            symbol=signal.symbol,
            strategy=signal.strategy,
            exchange=settings.exchange,
            product=settings.product,
            entry_price=signal.entry,
            quantity=quantity,
            sl=signal.sl,
            tp=signal.tp,
            entry_order_id=trade_result.order_id,
            sl_order_id=sl_order_id,
            tp_order_id=tp_order_id,
        ))

    # 10. Persist to DB
    save(signal, order, trade_result)


def main() -> None:
    """Entry point — start the signal engine with position tracker."""
    setup_logger()
    logger.info("Signal Engine starting")
    logger.info(f"Sizing mode: {settings.sizing_mode}")
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

        # Start position tracker in background
        tracker_task = asyncio.create_task(tracker.start())
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
            logger.info("Signal Engine stopped")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Signal Engine interrupted")
        sys.exit(0)


if __name__ == "__main__":
    main()
