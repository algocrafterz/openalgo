"""Broker instrument metadata, read from OpenAlgo's symbol master (SymToken).

The broker tick size is the tick source for anything price-grid dependent (value-area row
step, confluence epsilon). Lookups fail fast: a missing symbol or a non-positive tick raises
LookupError instead of silently substituting a default that would shift every price row.
"""

from __future__ import annotations

import os
import sqlite3
from functools import lru_cache
from pathlib import Path

DEFAULT_DB = "db/openalgo.db"


@lru_cache(maxsize=8192)
def _lookup(db_path: str, symbol: str, exchange: str) -> float:
    path = Path(db_path)
    if not path.exists():
        raise LookupError(f"OpenAlgo database not found: {db_path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select tick_size from symtoken where symbol = ? and exchange = ? limit 1",
            (symbol, exchange),
        ).fetchone()
    except sqlite3.Error as exc:
        raise LookupError(f"cannot read symtoken from {db_path}: {exc}") from exc
    finally:
        con.close()
    if row is None or row[0] is None or float(row[0]) <= 0:
        raise LookupError(f"no positive tick_size for {symbol} on {exchange} in {db_path}")
    return float(row[0])


def tick_size(symbol: str, exchange: str = "NSE", db_path: str | None = None) -> float:
    """Broker tick size for `symbol`; raises LookupError when it cannot be established."""
    return _lookup(db_path or os.environ.get("OPENALGO_DB", DEFAULT_DB), symbol, exchange)


#: The previous month's closing price fixes this month's tier, and a stock can move a long way in
#: a month (a 23% fall makes last month's close 1.5x today's price), so order prices are built
#: for the coarsest tier a price this far above the current one could carry.
TIER_DRIFT = 1.5


def tick_for_price(price: float) -> float:
    """NSE cash-segment tick for a reference price (NSE/CMTR/67133, 13 Mar 2025).

    Below Rs 250: 0.01; Rs 250 to 1,000: 0.05; above 1,000 to 5,000: 0.10; above 5,000 to
    10,000: 0.50; above 10,000 to 20,000: 1.00; above 20,000: 5.00. A band edge belongs to the
    finer tier. The exchange picks the tier from the previous month's last close, not from the
    live price - use `safe_tick` when only the live price is known.
    """
    if not price > 0:
        raise ValueError(f"price must be positive, got {price}")
    if price < 250:
        return 0.01
    if price <= 1000:
        return 0.05
    if price <= 5000:
        return 0.10
    if price <= 10000:
        return 0.50
    if price <= 20000:
        return 1.00
    return 5.00


def safe_tick(price: float) -> float:
    """A tick that is valid under every tier the stock could be in this month.

    Every tier's tick is a multiple of the finer ones (0.10 = 2 x 0.05, 0.50 = 5 x 0.10, ...), so a
    price on the coarsest plausible tick is also on the tick of any finer tier. That makes the
    result safe for order and trigger prices without knowing last month's close, at the cost of
    at most a few paise of precision.
    """
    return tick_for_price(price * TIER_DRIFT)
