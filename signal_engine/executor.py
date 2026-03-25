"""Order construction and OpenAlgo API integration."""

import asyncio
import math
from typing import Optional, Tuple

import httpx
from loguru import logger

from signal_engine.config import settings
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult


# NSE equity tick size is 0.05
_TICK_SIZE = 0.05


def round_to_tick(price: float, direction: str = "nearest") -> float:
    """Round price to valid NSE tick size (0.05).

    direction: 'nearest', 'down', or 'up'
    """
    if direction == "down":
        return round(math.floor(price / _TICK_SIZE) * _TICK_SIZE, 2)
    elif direction == "up":
        return round(math.ceil(price / _TICK_SIZE) * _TICK_SIZE, 2)
    else:
        return round(round(price / _TICK_SIZE) * _TICK_SIZE, 2)


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

            # Broker may return status=success but with null orderid
            # when the order was actually rejected at broker level
            raw_order_id = data.get("orderid")
            if raw_order_id is None:
                reason = data.get("message", "Order returned success but no order ID")
                logger.warning(f"Order rejected for {order.symbol}: {reason}")
                return TradeResult(
                    status=OrderStatus.REJECTED,
                    message=reason,
                )

            return TradeResult(
                order_id=str(raw_order_id),
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

    # Round trigger price to valid tick — conservative direction
    # LONG SL: round down (triggers earlier), SHORT SL: round up (triggers earlier)
    sl_direction = "down" if signal.direction == Direction.LONG else "up"
    trigger_price = round_to_tick(signal.sl, sl_direction)

    return Order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        action=action,
        quantity=quantity,
        price=0.0,
        order_type=sl_order_type,
        product=signal.product or settings.product,
        strategy_tag=signal.strategy,
        trigger_price=trigger_price,
    )


def build_tp_order(signal: Signal, quantity: int) -> Order:
    """Build a take-profit leg order for a bracket.

    For LONG entry: SELL LIMIT at price=signal.tp
    For SHORT entry: BUY LIMIT at price=signal.tp
    """
    action = Action.SELL if signal.direction == Direction.LONG else Action.BUY

    # Round TP price to valid tick — conservative direction
    # LONG TP: round down (fills sooner), SHORT TP: round up (fills sooner)
    tp_direction = "down" if signal.direction == Direction.LONG else "up"
    tp_price = round_to_tick(signal.tp, tp_direction)

    return Order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        action=action,
        quantity=quantity,
        price=tp_price,
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
    """Place SL leg only for a bracket order.

    Indian brokers treat the first SELL as an exit from the long, and any second
    SELL (even a TP LIMIT) as a new short position requiring full margin. Placing
    both SL and TP simultaneously therefore always results in one of them being
    rejected with FUND LIMIT INSUFFICIENT.

    Strategy: Place only the SL order here (safety-critical). TP is handled by
    the position tracker, which monitors LTP via positionbook and exits at market
    when the TP price is reached.

    Returns (sl_result, None) always — tp_result slot reserved for future use.
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
        if attempt < settings.bracket_max_sl_retries:
            await asyncio.sleep(settings.bracket_retry_delay)

    if sl_result.status != OrderStatus.SUCCESS:
        logger.error(
            f"SL leg failed after {settings.bracket_max_sl_retries} attempts for "
            f"{signal.symbol} - position has no SL protection"
        )

    # TP is handled by the position tracker via LTP monitoring (not a broker order)
    return sl_result, None
