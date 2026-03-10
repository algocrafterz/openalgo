"""Order construction and OpenAlgo API integration."""

from typing import Optional, Tuple

import httpx
from loguru import logger

from signal_engine.config import settings
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult


def build_order(
    signal: Signal,
    quantity: int,
    order_type: str = "",
    exchange: str = "",
    product: str = "",
) -> Order:
    """Convert a Signal + quantity into an Order ready for OpenAlgo."""
    action = Action.BUY if signal.direction == Direction.LONG else Action.SELL
    otype = order_type or settings.order_type
    price = 0.0 if otype == "MARKET" else signal.entry

    return Order(
        symbol=signal.symbol,
        exchange=exchange or signal.exchange or settings.exchange,
        action=action,
        quantity=quantity,
        price=price,
        order_type=otype,
        product=product or signal.product or settings.product,
        strategy_tag=signal.strategy,
    )


async def send_order(order: Order) -> TradeResult:
    """Send order to OpenAlgo REST API asynchronously."""
    url = f"{settings.openalgo_base_url}/api/v1/placeorder"
    payload = {
        "apikey": settings.openalgo_api_key,
        "strategy": order.strategy_tag,
        "symbol": order.symbol,
        "action": order.action.value,
        "exchange": order.exchange,
        "pricetype": order.order_type,
        "product": order.product,
        "quantity": order.quantity,
        "price": order.price,
    }
    if order.trigger_price > 0:
        payload["trigger_price"] = order.trigger_price

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)

            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}

            # Handle non-success responses without raise_for_status
            # so we can extract the JSON error message
            if response.status_code >= 400:
                reason = data.get("message", response.text)
                mode = data.get("mode", "")
                mode_tag = f" [{mode}]" if mode else ""
                logger.warning(f"Order rejected for {order.symbol}{mode_tag}: {reason}")
                return TradeResult(
                    status=OrderStatus.REJECTED,
                    message=reason,
                )

            if data.get("status") == "error":
                reason = data.get("message", "Unknown error")
                logger.warning(f"Order failed for {order.symbol}: {reason}")
                return TradeResult(
                    status=OrderStatus.REJECTED,
                    message=reason,
                )

            return TradeResult(
                order_id=str(data.get("orderid", "")),
                status=OrderStatus.SUCCESS,
                message=str(data.get("status", "")),
            )
    except httpx.TimeoutException:
        logger.error(f"Timeout sending order for {order.symbol}")
        return TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
    except Exception as e:
        logger.error(f"Error sending order for {order.symbol}: {e}")
        return TradeResult(status=OrderStatus.ERROR, message=str(e))


def build_sl_order(signal: Signal, quantity: int) -> Order:
    """Build a stop-loss leg order for a bracket.

    For LONG entry: SELL SL-M with trigger_price=signal.sl
    For SHORT entry: BUY SL-M with trigger_price=signal.sl
    """
    action = Action.SELL if signal.direction == Direction.LONG else Action.BUY
    sl_order_type = settings.bracket_sl_order_type

    return Order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        action=action,
        quantity=quantity,
        price=0.0,
        order_type=sl_order_type,
        product=signal.product or settings.product,
        strategy_tag=signal.strategy,
        trigger_price=signal.sl,
    )


def build_tp_order(signal: Signal, quantity: int) -> Order:
    """Build a take-profit leg order for a bracket.

    For LONG entry: SELL LIMIT at price=signal.tp
    For SHORT entry: BUY LIMIT at price=signal.tp
    """
    action = Action.SELL if signal.direction == Direction.LONG else Action.BUY

    return Order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        action=action,
        quantity=quantity,
        price=signal.tp,
        order_type="LIMIT",
        product=signal.product or settings.product,
        strategy_tag=signal.strategy,
        trigger_price=0.0,
    )


async def send_bracket_legs(
    signal: Signal,
    quantity: int,
    entry_order_id: str,
) -> Tuple[TradeResult, Optional[TradeResult]]:
    """Place SL and TP legs for a bracket order.

    SL is placed first (safety-critical) with up to bracket_max_sl_retries attempts.
    TP is only placed after SL succeeds.

    Returns (sl_result, tp_result). If SL fails, returns (sl_result, None).
    """
    sl_order = build_sl_order(signal, quantity)
    sl_result: TradeResult = TradeResult(status=OrderStatus.ERROR, message="Not attempted")

    for attempt in range(1, settings.bracket_max_sl_retries + 1):
        sl_result = await send_order(sl_order)
        if sl_result.status == OrderStatus.SUCCESS:
            logger.info(
                f"SL leg placed for {signal.symbol}: id={sl_result.order_id} (attempt {attempt})"
            )
            break
        logger.warning(
            f"SL leg attempt {attempt}/{settings.bracket_max_sl_retries} failed for "
            f"{signal.symbol}: {sl_result.message}"
        )

    if sl_result.status != OrderStatus.SUCCESS:
        logger.error(
            f"SL leg failed after {settings.bracket_max_sl_retries} attempts for "
            f"{signal.symbol} - skipping TP to avoid naked position"
        )
        return sl_result, None

    tp_order = build_tp_order(signal, quantity)
    tp_result = await send_order(tp_order)

    if tp_result.status == OrderStatus.SUCCESS:
        logger.info(f"TP leg placed for {signal.symbol}: id={tp_result.order_id}")
    else:
        logger.warning(f"TP leg failed for {signal.symbol}: {tp_result.message}")

    return sl_result, tp_result
