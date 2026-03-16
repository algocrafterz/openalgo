"""
Data loader for backtesting - reads directly from Historify DuckDB.

No Flask server required. Imports the existing historify_db module
and provides a clean interface for loading OHLCV data.
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytz

# Add project root to path so we can import database modules
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from database.historify_db import export_to_dataframe

IST = pytz.timezone("Asia/Kolkata")


def _date_to_epoch(date_str: str) -> int:
    """Convert date string (YYYY-MM-DD) to epoch timestamp."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt_ist = IST.localize(dt)
    return int(dt_ist.timestamp())


def load_ohlcv(
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    Load OHLCV data from Historify DuckDB.

    Args:
        symbol: Trading symbol (e.g., "SBIN")
        exchange: Exchange code (e.g., "NSE")
        interval: Candle interval (e.g., "5m", "15m", "1h", "D")
        start_date: Start date as "YYYY-MM-DD"
        end_date: End date as "YYYY-MM-DD"

    Returns:
        DataFrame with IST datetime index and columns: open, high, low, close, volume
        Empty DataFrame if no data found.

    Raises:
        ValueError: If no data found for the given parameters.
    """
    start_ts = _date_to_epoch(start_date)
    end_ts = _date_to_epoch(end_date) + 86400 - 1  # end of day

    df = export_to_dataframe(symbol, exchange, interval, start_ts, end_ts)

    if df.empty:
        raise ValueError(
            f"No data in Historify for {exchange}:{symbol} {interval} "
            f"from {start_date} to {end_date}. "
            f"Download data via the Historify web UI first."
        )

    # Ensure IST timezone
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)

    # Keep only OHLCV columns
    ohlcv_cols = ["open", "high", "low", "close", "volume"]
    available = [c for c in ohlcv_cols if c in df.columns]
    df = df[available]

    df = df.sort_index()
    return df


def load_ohlcv_raw(
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    Load raw OHLCV data (timezone-naive) for VectorBT compatibility.

    Same as load_ohlcv but strips timezone info since VectorBT
    doesn't handle timezone-aware indices well.
    """
    df = load_ohlcv(symbol, exchange, interval, start_date, end_date)
    df.index = df.index.tz_localize(None)
    return df
