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
    payload = _placeorder_payload(order)

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            return _interpret_placeorder(order, response, data)
    except httpx.TimeoutException:
        logger.error(f"Timeout sending order for {order.symbol}")
        return TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
    except Exception as e:
        logger.error(f"Error sending order for {order.symbol}: {e}")
        return TradeResult(status=OrderStatus.ERROR, message=str(e))


def _placeorder_payload(order: Order) -> dict:
    """Map an Order onto the OpenAlgo placeorder request body."""
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
    return payload


def _interpret_placeorder(order: Order, response, data: dict) -> TradeResult:
    """Turn a placeorder response into a TradeResult.

    Deliberately avoids raise_for_status so the JSON error message survives, and
    treats a success status with a null orderid as a rejection — some brokers answer
    that way when the order was refused downstream.
    """
    if response.status_code >= 400:
        reason = data.get("message", response.text)
        mode = data.get("mode", "")
        mode_tag = f" [{mode}]" if mode else ""
        logger.warning(f"Order rejected for {order.symbol}{mode_tag}: {reason}")
        return TradeResult(status=OrderStatus.REJECTED, message=reason)

    if data.get("status") == "error":
        reason = data.get("message", "Unknown error")
        logger.warning(f"Order failed for {order.symbol}: {reason}")
        return TradeResult(status=OrderStatus.REJECTED, message=reason)

    raw_order_id = data.get("orderid")
    if raw_order_id is None:
        reason = data.get("message", "Order returned success but no order ID")
        logger.warning(f"Order rejected for {order.symbol}: {reason}")
        return TradeResult(status=OrderStatus.REJECTED, message=reason)

    return TradeResult(
        order_id=str(raw_order_id),
        status=OrderStatus.SUCCESS,
        message=str(data.get("status", "")),
    )


def build_exit_order(
    symbol: str,
    exchange: str,
    quantity: int,
    product: str,
    strategy_tag: str,
    direction: "Direction" = None,
) -> Order:
    """Build a MARKET order to close an existing position.

    For LONG positions: SELL to close.
    For SHORT positions: BUY to cover.
    """
    from signal_engine.models import Direction as _Direction
    if direction is None:
        direction = _Direction.LONG
    action = Action.SELL if direction == _Direction.LONG else Action.BUY
    return Order(
        symbol=symbol,
        exchange=exchange,
        action=action,
        quantity=quantity,
        price=0.0,
        order_type="MARKET",
        product=product,
        strategy_tag=strategy_tag,
    )


async def place_sl_order(
    symbol: str,
    exchange: str,
    direction: "Direction",
    quantity: int,
    sl_price: float,
    product: str,
    strategy_tag: str,
) -> TradeResult:
    """Place a SL-M order with retries. Reusable for initial bracket and re-placement after partial exit.

    For LONG positions: SELL SL-M with trigger at sl_price (rounded up — fires earlier).
    For SHORT positions: BUY SL-M with trigger at sl_price (rounded down — fires earlier).

    Returns TradeResult — caller must check .status == OrderStatus.SUCCESS.
    """
    from signal_engine.models import Direction as _Direction

    action = Action.SELL if direction == _Direction.LONG else Action.BUY
    # Conservative rounding: trigger SL before the stated price to minimise loss
    # LONG SL is below entry — round UP so trigger fires before reaching stated price
    # SHORT SL is above entry — round DOWN so trigger fires before reaching stated price
    sl_direction = "up" if direction == _Direction.LONG else "down"
    trigger_price = round_to_tick(sl_price, sl_direction)

    sl_order = Order(
        symbol=symbol,
        exchange=exchange,
        action=action,
        quantity=quantity,
        price=0.0,
        order_type=settings.bracket_sl_order_type,
        product=product,
        strategy_tag=strategy_tag,
        trigger_price=trigger_price,
    )

    result: TradeResult = TradeResult(status=OrderStatus.ERROR, message="Not attempted")
    for attempt in range(1, settings.bracket_max_sl_retries + 1):
        result = await send_order(sl_order)
        if result.status == OrderStatus.SUCCESS:
            logger.info(f"SL placed for {symbol}: id={result.order_id} trigger={trigger_price} (attempt {attempt})")
            break
        logger.warning(
            f"SL attempt {attempt}/{settings.bracket_max_sl_retries} failed for {symbol}: {result.message}"
        )
        if attempt < settings.bracket_max_sl_retries:
            await asyncio.sleep(settings.bracket_retry_delay)

    if result.status != OrderStatus.SUCCESS:
        logger.error(f"SL failed after {settings.bracket_max_sl_retries} attempts for {symbol} — no SL protection")

    return result


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

    Strategy: Place only the SL-M order here (safety-critical). TP exit is driven
    by TradingView TP HIT signal -> _handle_exit pipeline.

    Returns (sl_result, None) always — tp_result slot reserved for future use.
    """
    sl_result = await place_sl_order(
        symbol=signal.symbol,
        exchange=signal.exchange or settings.exchange,
        direction=signal.direction,
        quantity=quantity,
        sl_price=signal.sl,
        product=signal.product or settings.product,
        strategy_tag=signal.strategy,
    )
    # TP exit via TradingView TP HIT signal -> _handle_exit (not a broker order)
    return sl_result, None
