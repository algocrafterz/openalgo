"""Inputs for regime classification: NIFTY daily closes and India VIX.

Historify holds NIFTY (NSE_INDEX) only as 1-minute bars, and holds no India VIX. So:

  NIFTY  session close taken from the last 1-minute bar of each COMPLETE session in
         Historify (broker data, preferred). Yahoo ^NSEI fills only the days Historify
         lacks: the 200-day warm-up before its first bar, and any gap since its last.
         On the days both exist the two are compared and the agreement is recorded.
  VIX    Yahoo ^INDIAVIX, the only source available. If it cannot be fetched the loader
         says so and the classifier falls back to NIFTY realised volatility.

The Yahoo fetch is injected (`fetch`) so tests never touch the network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from signal_engine.backtest.data import _DUCKDB_CONFIG, HISTORIFY_DB
from utils.logging import get_logger

logger = get_logger(__name__)

NIFTY_SYMBOL, NIFTY_EXCHANGE = "NIFTY", "NSE_INDEX"
YAHOO_NIFTY, YAHOO_VIX = "^NSEI", "^INDIAVIX"

#: 200 sessions of warm-up plus the year of percentile history needs about 2.5 years
#: before the first Historify bar (2020-01-01), so ask Yahoo for a generous start.
DEFAULT_START = "2015-01-01"

#: IST seconds-of-day bounds of the NSE continuous session, 09:15 and 15:29 inclusive.
_OPEN_SEC, _CLOSE_SEC = 9 * 3600 + 15 * 60, 15 * 3600 + 29 * 60
#: A session with fewer minute bars than this is partial (early close, feed outage).
MIN_SESSION_BARS = 300
_IST_OFFSET = 19800


@dataclass(frozen=True)
class RegimeInputs:
    close: pd.Series
    vix: pd.Series | None
    source: dict[str, Any]


def daily_close_from_historify(
    db_path: str | Path | None = None,
    *,
    symbol: str = NIFTY_SYMBOL,
    exchange: str = NIFTY_EXCHANGE,
    min_bars: int = MIN_SESSION_BARS,
) -> pd.Series:
    """Session close per IST date from 1-minute bars; partial sessions are skipped."""
    import duckdb

    con = duckdb.connect(str(db_path or HISTORIFY_DB), read_only=True, config=_DUCKDB_CONFIG)
    try:
        rows = con.execute(
            "SELECT CAST(epoch_ms((timestamp + ?) * 1000) AS DATE) AS d, "
            "arg_max(close, timestamp) AS c, count(*) AS n FROM market_data "
            "WHERE symbol = ? AND exchange = ? AND interval = '1m' "
            "AND ((timestamp + ?) % 86400) BETWEEN ? AND ? "
            "GROUP BY d HAVING count(*) >= ? ORDER BY d",
            [_IST_OFFSET, symbol, exchange, _IST_OFFSET, _OPEN_SEC, _CLOSE_SEC, min_bars],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series(
        [r[1] for r in rows], index=pd.DatetimeIndex([r[0] for r in rows]), dtype=float
    )


def fetch_yahoo_close(ticker: str, start: str) -> pd.Series:
    """Daily closes from Yahoo as a tz-naive, date-indexed series."""
    import yfinance as yf

    df = yf.download(ticker, start=start, progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo returned no data for {ticker}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna().astype(float)
    index = pd.DatetimeIndex(close.index)
    if index.tz is not None:
        index = index.tz_convert("Asia/Kolkata").tz_localize(None)
    close.index = index.normalize()
    return close[~close.index.duplicated(keep="last")].sort_index()


def _cross_check(broker: pd.Series, yahoo: pd.Series) -> dict[str, Any]:
    common = broker.index.intersection(yahoo.index)
    if common.empty:
        return {"days": 0, "max_abs_pct_diff": None, "share_within_0_1pct": None}
    diff = ((broker.loc[common] - yahoo.loc[common]) / yahoo.loc[common]).abs()
    return {
        "days": int(len(common)),
        "max_abs_pct_diff": round(float(diff.max()), 5),
        "share_within_0_1pct": round(float((diff <= 0.001).mean()), 4),
    }


def _day(ts: pd.Timestamp) -> str:
    return ts.date().isoformat()


def load_regime_inputs(
    db_path: str | Path | None = None,
    *,
    start: str = DEFAULT_START,
    fetch: Callable[[str, str], pd.Series] = fetch_yahoo_close,
) -> RegimeInputs:
    """NIFTY closes (broker first, Yahoo for gaps) and India VIX, with a source record."""
    broker = daily_close_from_historify(db_path)
    yahoo_nifty = pd.Series(dtype=float)
    try:
        yahoo_nifty = fetch(YAHOO_NIFTY, start)
    except Exception:
        logger.exception("Yahoo %s fetch failed; regime uses Historify closes only", YAHOO_NIFTY)

    missing = yahoo_nifty.index.difference(broker.index)
    close = pd.concat([broker, yahoo_nifty.loc[missing]]).sort_index()
    if close.empty:
        raise RuntimeError(
            f"no {NIFTY_SYMBOL} daily closes: Historify has none and Yahoo {YAHOO_NIFTY} failed"
        )

    vix: pd.Series | None = None
    vix_source = "unavailable"
    try:
        vix = fetch(YAHOO_VIX, start)
        vix_source = f"yahoo {YAHOO_VIX}"
    except Exception:
        logger.exception("Yahoo %s fetch failed; regime volatility uses realised vol", YAHOO_VIX)

    source: dict[str, Any] = {
        "nifty_historify_days": int(len(broker)),
        "nifty_yahoo_days": int(len(missing)),
        "nifty_from": _day(close.index[0]),
        "nifty_to": _day(close.index[-1]),
        "nifty_cross_check": _cross_check(broker, yahoo_nifty),
        "vix_source": vix_source,
    }
    if vix is not None and len(vix):
        source["vix_from"] = _day(vix.index[0])
        source["vix_to"] = _day(vix.index[-1])
    return RegimeInputs(close=close, vix=vix, source=source)
