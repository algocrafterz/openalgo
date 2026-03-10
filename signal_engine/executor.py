"""Order construction and OpenAlgo API integration."""

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
