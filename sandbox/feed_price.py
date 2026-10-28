"""A live-feed price for a symbol, when one is safe to use.

Used by the quotes service to answer an opt-in request (``max_age_seconds``) from the feed
instead of the broker. A symbol that is already on the live feed (an open position) has a
price in the market-data cache. The price is used only when:

* the market-data service reports the feed healthy and its data flowing
  (``is_trade_management_safe``), and
* the symbol's last tick arrived no more than ``max_age`` seconds ago.

Timestamp semantics, verified in ``services/market_data_service.py``: ``last_update`` is the
time this process *received* the packet, truncated to whole seconds, and is refreshed on every
packet of any mode. Truncation can only make a tick look up to one second older than it is,
so the bound errs on the side of refusing. It is receipt time, not exchange time.

The feed carries a last price only: no bid or ask. Sandbox order pricing and fills therefore do
NOT use it: they fill a BUY at the ask and a SELL at the bid from a REST quote.

Every refusal returns None and the caller continues on its existing REST path and failure
policy. Nothing here ever raises.
"""

from __future__ import annotations

import time

from services.market_data_service import get_market_data_service
from utils.logging import get_logger

logger = get_logger(__name__)

#: A tick stamped further ahead than this means the clock moved; refuse it.
_CLOCK_SKEW_TOLERANCE_SECONDS = 1.0


def feed_quote(symbol: str, exchange: str, max_age: float) -> dict | None:
    """A quote-shaped dict from the live feed, or None when it cannot be trusted."""
    if max_age <= 0:
        return None
    try:
        service = get_market_data_service()
        safe, reason = service.is_trade_management_safe()
        if not safe:
            logger.debug(f"Feed pricing skipped for {symbol}: {reason}")
            return None

        data = service.get_all_data(symbol, exchange)
        if not data:
            return None

        age = time.time() - float(data.get("last_update") or 0)
        if age < -_CLOCK_SKEW_TOLERANCE_SECONDS or age > max_age:
            logger.debug(f"Feed pricing skipped for {symbol}: tick age {age:.1f}s")
            return None

        ltp_entry = data.get("ltp")
        ltp = ltp_entry.get("value") if isinstance(ltp_entry, dict) else None
        if not isinstance(ltp, (int, float)) or ltp <= 0:
            return None

        return {
            "ltp": float(ltp),
            "bid": 0,
            "ask": 0,
            "source": "feed",
            "age": max(age, 0.0),
        }
    except Exception:
        logger.debug(f"Feed pricing unavailable for {symbol}", exc_info=True)
        return None
