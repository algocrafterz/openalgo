"""Async wrapper for OpenAlgo REST API calls.

All endpoints respect OpenAlgo's analyze mode automatically:
- Live mode: returns real broker data
- Analyze mode: returns sandbox data (1Cr virtual capital)
"""

import asyncio
from typing import Tuple

import httpx
from loguru import logger

from signal_engine.config import settings


class MarginAPIError(Exception):
    """Raised when the Margin API fails after all retries."""


async def fetch_trading_mode() -> Tuple[str, bool]:
    """Check if OpenAlgo is in live or analyze mode.

    Returns (mode_str, is_analyze) e.g. ("analyze", True) or ("live", False).
    Returns ("unknown", False) on failure.
    """
    url = f"{settings.openalgo_base_url}/api/v1/analyzer"
    payload = {"apikey": settings.openalgo_api_key}

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "success":
                return ("unknown", False)
            mode_data = data.get("data", {})
            is_analyze = bool(mode_data.get("analyze_mode", False))
            mode_str = mode_data.get("mode", "unknown")
            return (mode_str, is_analyze)
    except Exception as e:
        logger.error(f"Failed to fetch trading mode: {e}")
        return ("unknown", False)


async def fetch_available_capital() -> float:
    """Fetch available capital for position sizing.

    Priority:
    1. If sandbox_capital is set in config and mode is analyze -> use override
    2. Otherwise fetch from OpenAlgo /api/v1/funds (live or sandbox)
    Returns 0.0 on failure.
    """
    # Check for sandbox capital override
    if settings.sandbox_capital > 0:
        _, is_analyze = await fetch_trading_mode()
        if is_analyze:
            logger.info(f"Using sandbox capital override: {settings.sandbox_capital:,.2f} INR")
            return settings.sandbox_capital

    url = f"{settings.openalgo_base_url}/api/v1/funds"
    payload = {"apikey": settings.openalgo_api_key}

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "success":
                logger.warning(f"Funds API returned non-success: {data}")
                return 0.0
            available = float(data.get("data", {}).get("availablecash", 0))
            logger.debug(f"Available capital: {available:,.2f} INR")
            return available
    except Exception as e:
        logger.error(f"Failed to fetch funds: {e}")
        return 0.0


async def fetch_open_position(symbol: str, strategy: str, exchange: str, product: str) -> int:
    """Fetch open position quantity for a specific symbol+strategy.

    Returns quantity (0 means position closed). Returns -1 on API error.
    """
    url = f"{settings.openalgo_base_url}/api/v1/openposition"
    payload = {
        "apikey": settings.openalgo_api_key,
        "strategy": strategy,
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "success":
                return -1
            return int(data.get("quantity", 0))
    except Exception as e:
        logger.error(f"Failed to fetch position for {symbol}: {e}")
        return -1


async def cancel_order(order_id: str, strategy: str) -> bool:
    """Cancel a pending order by order ID.

    Returns True on success, False on any failure.
    """
    url = f"{settings.openalgo_base_url}/api/v1/cancelorder"
    payload = {
        "apikey": settings.openalgo_api_key,
        "strategy": strategy,
        "orderid": order_id,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(f"Cancel order {order_id} returned HTTP {response.status_code}")
                return False
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if data.get("status") != "success":
                logger.warning(f"Cancel order {order_id} returned non-success: {data.get('message', 'unknown')}")
                return False
            return True
    except Exception as e:
        logger.error(f"Failed to cancel order {order_id}: {e}")
        return False


async def fetch_order_status(order_id: str, strategy: str) -> str:
    """Fetch current status of an order.

    Returns the orderstatus string (e.g. "complete", "pending") or "" on error.
    """
    url = f"{settings.openalgo_base_url}/api/v1/orderstatus"
    payload = {
        "apikey": settings.openalgo_api_key,
        "strategy": strategy,
        "orderid": order_id,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "success":
                logger.warning(f"Order status {order_id} returned non-success: {data}")
                return ""
            return str(data.get("data", {}).get("orderstatus", ""))
    except Exception as e:
        logger.error(f"Failed to fetch order status for {order_id}: {e}")
        return ""


async def fetch_positionbook():
    """Fetch all open positions in a single API call.

    Returns list of position dicts on success, None on failure.
    Each dict has: symbol, exchange, product, quantity, pnl, average_price, ltp.
    """
    url = f"{settings.openalgo_base_url}/api/v1/positionbook"
    payload = {"apikey": settings.openalgo_api_key}

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "success":
                logger.warning(f"Positionbook API returned non-success: {data}")
                return None
            return data.get("data", [])
    except Exception as e:
        logger.error(f"Failed to fetch positionbook: {e}")
        return None


async def close_all_positions(strategy: str) -> bool:
    """Close all open positions for a strategy via OpenAlgo API.

    Returns True on success, False on failure.
    """
    url = f"{settings.openalgo_base_url}/api/v1/closeposition"
    payload = {
        "apikey": settings.openalgo_api_key,
        "strategy": strategy,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(f"Close positions returned HTTP {response.status_code}")
                return False
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if data.get("status") == "error":
                logger.warning(f"Close positions failed: {data.get('message', 'unknown')}")
                return False
            return True
    except Exception as e:
        logger.error(f"Failed to close all positions: {e}")
        return False


async def cancel_all_orders(strategy: str) -> bool:
    """Cancel all pending orders for a strategy via OpenAlgo API.

    Returns True on success, False on failure.
    """
    url = f"{settings.openalgo_base_url}/api/v1/cancelallorder"
    payload = {
        "apikey": settings.openalgo_api_key,
        "strategy": strategy,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(f"Cancel all orders returned HTTP {response.status_code}")
                return False
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if data.get("status") == "error":
                logger.warning(f"Cancel all orders failed: {data.get('message', 'unknown')}")
                return False
            return True
    except Exception as e:
        logger.error(f"Failed to cancel all orders: {e}")
        return False


async def fetch_realised_pnl() -> float:
    """Fetch day's realised P&L from funds endpoint.

    Returns m2mrealized value or 0.0 on failure.
    """
    url = f"{settings.openalgo_base_url}/api/v1/funds"
    payload = {"apikey": settings.openalgo_api_key}

    try:
        async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "success":
                return 0.0
            return float(data.get("data", {}).get("m2mrealized", 0))
    except Exception as e:
        logger.error(f"Failed to fetch realised PnL: {e}")
        return 0.0


async def fetch_margin(
    symbol: str,
    exchange: str,
    action: str,
    quantity: int,
    product: str,
    pricetype: str = "MARKET",
    price: float = 0.0,
) -> float:
    """Fetch actual broker margin for a proposed order via OpenAlgo Margin API.

    Returns total_margin_required in INR.
    Retries on network/timeout failures. Raises MarginAPIError on all failures — no fallback.
    """
    url = f"{settings.openalgo_base_url}/api/v1/margin"
    payload = {
        "apikey": settings.openalgo_api_key,
        "positions": [{
            "exchange": exchange,
            "symbol": symbol,
            "action": action,
            "quantity": str(quantity),
            "product": product,
            "pricetype": pricetype,
            "price": str(price),
            "trigger_price": "0",
        }],
    }

    for attempt in range(1, settings.margin_api_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.api_timeout) as client:
                response = await client.post(url, json=payload)

            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}

            if response.status_code >= 400:
                reason = data.get("message", response.text)
                raise MarginAPIError(f"HTTP {response.status_code}: {reason}")

            if data.get("status") != "success":
                raise MarginAPIError(f"Margin API: {data.get('message', 'unknown error')}")

            margin = float(data["data"]["total_margin_required"])
            logger.debug(f"Margin for {symbol} qty={quantity}: {margin:,.2f}")
            return margin

        except MarginAPIError:
            raise  # Application-level errors: no retry (bad symbol, wrong exchange, etc.)
        except Exception as e:
            logger.warning(f"Margin API attempt {attempt}/{settings.margin_api_retries} for {symbol}: {e}")
            if attempt < settings.margin_api_retries:
                await asyncio.sleep(settings.bracket_retry_delay)

    raise MarginAPIError(f"Margin API failed after {settings.margin_api_retries} attempts for {symbol}")
