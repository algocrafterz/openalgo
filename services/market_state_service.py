"""Market state of each trading day, from NIFTY 50 daily bars, for retrospective analytics.

This module only labels days and groups trades that other services have already
costed. It never changes a signal, an order, a size, a stop, a charge or a P&L.

Benchmark
    NIFTY (exchange NSE_INDEX, interval D) from Historify, read through
    ``database.historify_db.get_ohlcv``. The helper opens one short DuckDB
    connection per call and closes it, so no handle or write lock is held. One
    benchmark is used for every strategy, whatever index the strategy trades.

Cleaning
    Bars are sorted by date and de-duplicated (last one wins). A bar with
    volume <= 0 (a provisional bar of the running session) or a missing,
    NaN or non-positive open, high, low or close is dropped. Bars are never
    built from one-minute data and no holiday table is used.

Trend (bar D, using bars up to and including D only)
    Uptrend    close > SMA20 and close > SMA50 and SMA20[D] > SMA20[D - 5 bars]
    Downtrend  close < SMA20 and close < SMA50 and SMA20[D] < SMA20[D - 5 bars]
    Range      anything else.
    Range is a RESIDUAL class: it means "neither Uptrend nor Downtrend". It is
    not a confirmed range-bound market. Comparisons are strict, so equality is
    Range. Needs MIN_BARS_TREND bars, else the trend is not classified.

Volatility (bar D, using bars up to and including D only)
    ATR(14) of Wilder (RMA, ``ewm(alpha=1/14, adjust=False)``, the same maths as
    ``signal_engine.features.price.atr``) divided by close. The value is ranked
    as its percentile within the trailing PERCENTILE_WINDOW bars, the window
    including bar D: the share of window values that are <= the current one.
    percentile <= 0.25 is Low, >= 0.75 is High, else Normal. Needs
    MIN_BARS_VOLATILITY bars (window plus ATR seed), else it is not classified.

Attribution
    A trade session date T takes the label of the latest valid daily bar dated
    strictly before T. Bar T itself is never used, because it is not complete
    during the session. If that bar is more than MAX_BAR_AGE_DAYS calendar days
    before T (a hole in the data) the day is Unclassified with a reason code.
    Missing data is never mapped to a class.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

BENCHMARK_SYMBOL = "NIFTY"
BENCHMARK_EXCHANGE = "NSE_INDEX"
BENCHMARK_INTERVAL = "D"
BENCHMARK_LABEL = "NIFTY 50 (daily)"

SMA_FAST = 20
SMA_SLOW = 50
SLOPE_LAG_BARS = 5
MIN_BARS_TREND = SMA_SLOW + SLOPE_LAG_BARS  # 55

ATR_PERIOD = 14
PERCENTILE_WINDOW = 252
MIN_BARS_VOLATILITY = PERCENTILE_WINDOW + ATR_PERIOD  # 266
LOW_PERCENTILE = 0.25
HIGH_PERCENTILE = 0.75

MAX_BAR_AGE_DAYS = 5
SMALL_SAMPLE_TRADES = 30
SMALL_SAMPLE_TEXT = "Small sample: do not draw conclusions"

UNCLASSIFIED = "Unclassified"
TREND_CLASSES = ("Uptrend", "Downtrend", "Range")
VOLATILITY_CLASSES = ("Low", "Normal", "High")
DIMENSIONS = {"trend": TREND_CLASSES, "volatility": VOLATILITY_CLASSES}

REASON_NO_BARS = "no_bars"
REASON_NO_PRIOR_BAR = "no_prior_bar"
REASON_STALE_BAR = "stale_bar"
REASON_INSUFFICIENT_HISTORY = "insufficient_history"
REASON_BAD_DATE = "bad_date"

_IST = timezone(timedelta(hours=5, minutes=30))
_BAR_COLUMNS = ("open", "high", "low", "close")


def methodology() -> dict[str, Any]:
    """The rules and constants, for the response and the page."""
    return {
        "benchmark": BENCHMARK_LABEL,
        "trend": (
            f"Uptrend: close above SMA{SMA_FAST} and SMA{SMA_SLOW}, and SMA{SMA_FAST} higher than "
            f"{SLOPE_LAG_BARS} bars earlier. Downtrend: the mirror. Range: neither (a residual "
            "class, not a confirmed range-bound market)."
        ),
        "volatility": (
            f"ATR({ATR_PERIOD}) divided by close, ranked within the last {PERCENTILE_WINDOW} "
            f"daily bars. Low: at or below the {int(LOW_PERCENTILE * 100)}th percentile. High: at "
            f"or above the {int(HIGH_PERCENTILE * 100)}th. Normal: between."
        ),
        "attribution": (
            "Each trade takes the label of the previous completed trading day before its "
            f"session date. If that day is more than {MAX_BAR_AGE_DAYS} calendar days earlier, or "
            "history is too short, the trade is Unclassified."
        ),
        "min_bars_trend": MIN_BARS_TREND,
        "min_bars_volatility": MIN_BARS_VOLATILITY,
        "max_bar_age_days": MAX_BAR_AGE_DAYS,
        "small_sample_trades": SMALL_SAMPLE_TRADES,
        "small_sample_text": SMALL_SAMPLE_TEXT,
    }


# ---------------------------------------------------------------- reading bars


def read_benchmark_bars() -> pd.DataFrame:
    """Raw NIFTY daily bars from Historify (timestamp, open, high, low, close, volume, oi).

    ``get_ohlcv`` opens one DuckDB connection, closes it and returns an empty
    frame when the file cannot be read. Nothing is written.
    """
    from database.historify_db import get_ohlcv

    return get_ohlcv(BENCHMARK_SYMBOL, BENCHMARK_EXCHANGE, BENCHMARK_INTERVAL)


def _bar_date(value: Any) -> date | None:
    try:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, (int, float, np.integer, np.floating)):
            if not math.isfinite(float(value)):
                return None
            return datetime.fromtimestamp(int(value), tz=_IST).date()
        return date.fromisoformat(str(value)[:10])
    except (ValueError, OverflowError, OSError):
        return None


def clean_bars(raw: pd.DataFrame | None) -> pd.DataFrame:
    """Valid daily bars, one per date, ascending, indexed by position.

    Accepts a ``timestamp`` column (epoch seconds) or a ``date`` column.
    Returns columns date, open, high, low, close, volume.
    """
    columns = ["date", *_BAR_COLUMNS, "volume"]
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(index=range(len(raw)))
    source = raw["date"] if "date" in raw.columns else raw["timestamp"]
    frame["date"] = [_bar_date(v) for v in source.tolist()]
    for name in (*_BAR_COLUMNS, "volume"):
        frame[name] = pd.to_numeric(raw[name].reset_index(drop=True), errors="coerce")
    frame = frame.dropna(subset=["date", *_BAR_COLUMNS, "volume"])
    frame = frame[np.isfinite(frame[list(_BAR_COLUMNS)]).all(axis=1)]
    frame = frame[(frame[list(_BAR_COLUMNS)] > 0).all(axis=1) & (frame["volume"] > 0)]
    frame = frame.sort_values("date", kind="stable").drop_duplicates("date", keep="last")
    return frame.reset_index(drop=True)[columns]


# ---------------------------------------------------------------- classification


def atr_series(high: pd.Series, low: pd.Series, close: pd.Series, n: int = ATR_PERIOD) -> pd.Series:
    """Wilder ATR: RMA of the true range (same maths as signal_engine.features.price.atr)."""
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / n, adjust=False).mean()


def trend_labels(close: pd.Series) -> list[str | None]:
    """Trend label for every bar, each using that bar and earlier bars only."""
    sma_fast = close.rolling(SMA_FAST).mean()
    sma_slow = close.rolling(SMA_SLOW).mean()
    slope = sma_fast - sma_fast.shift(SLOPE_LAG_BARS)
    up = (close > sma_fast) & (close > sma_slow) & (slope > 0)
    down = (close < sma_fast) & (close < sma_slow) & (slope < 0)
    out: list[str | None] = []
    for i in range(len(close)):
        if i + 1 < MIN_BARS_TREND:
            out.append(None)
        elif up.iloc[i]:
            out.append("Uptrend")
        elif down.iloc[i]:
            out.append("Downtrend")
        else:
            out.append("Range")
    return out


def volatility_percentiles(bars: pd.DataFrame) -> np.ndarray:
    """Percentile of ATR/close within the trailing window (current bar included).

    NaN until MIN_BARS_VOLATILITY bars exist, so the ATR seed is excluded.
    """
    count = len(bars)
    out = np.full(count, np.nan)
    if count < MIN_BARS_VOLATILITY:
        return out
    ratio = (atr_series(bars["high"], bars["low"], bars["close"]) / bars["close"]).to_numpy()
    windows = np.lib.stride_tricks.sliding_window_view(ratio, PERCENTILE_WINDOW)
    ranks = (windows <= windows[:, -1:]).mean(axis=1)
    out[PERCENTILE_WINDOW - 1 :] = ranks
    out[: MIN_BARS_VOLATILITY - 1] = np.nan
    return out


def volatility_label(percentile: float) -> str | None:
    if percentile is None or not math.isfinite(percentile):
        return None
    if percentile <= LOW_PERCENTILE:
        return "Low"
    if percentile >= HIGH_PERCENTILE:
        return "High"
    return "Normal"


class MarketStates:
    """Trend and volatility label of every valid bar, plus attribution by date."""

    def __init__(self, raw: pd.DataFrame | None):
        raw_rows = 0 if raw is None else len(raw)
        bars = clean_bars(raw)
        self.dropped_bars = raw_rows - len(bars)
        self.dates: list[date] = list(bars["date"])
        self.trend: list[str | None] = trend_labels(bars["close"]) if len(bars) else []
        self.volatility: list[str | None] = (
            [volatility_label(p) for p in volatility_percentiles(bars)] if len(bars) else []
        )

    def at_bar(self, index: int) -> dict[str, Any]:
        return {"trend": self.trend[index], "volatility": self.volatility[index]}

    def label_for(self, session_date: date) -> dict[str, Any]:
        """Label applying to a session date: the latest valid bar strictly before it."""
        if not self.dates:
            return _unclassified(REASON_NO_BARS)
        index = bisect.bisect_left(self.dates, session_date) - 1
        if index < 0:
            return _unclassified(REASON_NO_PRIOR_BAR)
        bar = self.dates[index]
        if (session_date - bar).days > MAX_BAR_AGE_DAYS:
            return _unclassified(REASON_STALE_BAR, as_of=bar)
        trend, vol = self.trend[index], self.volatility[index]
        if trend is None and vol is None:
            return _unclassified(REASON_INSUFFICIENT_HISTORY, as_of=bar)
        both = trend is not None and vol is not None
        return {
            "trend": trend,
            "volatility": vol,
            "status": "classified" if both else "partial",
            "as_of": bar.isoformat(),
            "reason": None if both else REASON_INSUFFICIENT_HISTORY,
        }

    def status(self) -> dict[str, Any]:
        return {
            "state": "ok" if self.dates else "no_data",
            "bars": len(self.dates),
            "dropped_bars": self.dropped_bars,
            "first_bar": self.dates[0].isoformat() if self.dates else None,
            "last_bar": self.dates[-1].isoformat() if self.dates else None,
        }


def _unclassified(reason: str, as_of: date | None = None) -> dict[str, Any]:
    return {
        "trend": None,
        "volatility": None,
        "status": "unclassified",
        "as_of": as_of.isoformat() if as_of else None,
        "reason": reason,
    }


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def classify_dates_with_status(
    dates: Iterable[Any], *, bar_reader: Callable[[], pd.DataFrame] | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """``classify_dates`` plus the status of the benchmark data that was read."""
    states = MarketStates((bar_reader or read_benchmark_bars)())
    labels: dict[str, dict[str, Any]] = {}
    for value in dates:
        parsed = _to_date(value)
        key = parsed.isoformat() if parsed else str(value)
        labels[key] = states.label_for(parsed) if parsed else _unclassified(REASON_BAD_DATE)
    return labels, states.status()


def classify_dates(
    dates: Iterable[Any], *, bar_reader: Callable[[], pd.DataFrame] | None = None
) -> dict[str, dict[str, Any]]:
    """{date_iso: {trend, volatility, status, as_of, reason}} for trade session dates.

    ``trend`` and ``volatility`` are None when not classified. ``bar_reader`` returns
    raw daily bars (timestamp or date, open, high, low, close, volume); the default
    reads Historify.
    """
    return classify_dates_with_status(dates, bar_reader=bar_reader)[0]


# ---------------------------------------------------------------- aggregation


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _money(value: float) -> float:
    return round(value, 2)


def _state_row(name: str, rows: list[dict]) -> dict[str, Any]:
    """One class row from per-trade rows that already carry gross P&L and cost.

    Nothing is recomputed: gross is each trade's ``realized_pnl`` and the cost is
    the ``cost`` the trade already has (None when it is not costed). Net figures
    exist only when every trade of the class is costed.
    """
    total = len(rows)
    row: dict[str, Any] = {
        "name": name,
        "trades": total,
        "wins": 0,
        "win_rate": None,
        "gross_pnl": None,
        "charges_estimated": None,
        "net_pnl": None,
        "avg_net_per_trade": None,
        "profit_factor": None,
        "net_coverage": {"costed": 0, "total": total},
        "small_sample": total < SMALL_SAMPLE_TRADES,
    }
    if not total:
        return row
    gross = [float(r["realized_pnl"]) for r in rows]
    costs = [_finite(r.get("cost")) for r in rows]
    known = [c for c in costs if c is not None]
    row["wins"] = sum(1 for g in gross if g > 0)
    row["win_rate"] = _money(row["wins"] / total * 100)
    row["gross_pnl"] = _money(sum(gross))
    row["charges_estimated"] = _money(sum(known))
    row["net_coverage"] = {"costed": len(known), "total": total}
    if len(known) == total:
        net = [g - c for g, c in zip(gross, costs, strict=True)]
        row["net_pnl"] = _money(sum(net))
        row["avg_net_per_trade"] = _money(sum(net) / total)
        profit = sum(n for n in net if n > 0)
        loss = -sum(n for n in net if n < 0)
        if profit > 0 and loss > 0:
            row["profit_factor"] = round(profit / loss, 2)
    return row


def group_by_state(
    trades: list[dict], labels: dict[str, dict[str, Any]], dimension: str
) -> list[dict[str, Any]]:
    """Per-class rows for ``dimension`` ('trend' or 'volatility').

    Every class and 'Unclassified' is listed, in a fixed order, so a trade is in
    exactly one row. ``trades`` need ``trade_date``, ``realized_pnl`` and ``cost``.
    """
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {sorted(DIMENSIONS)}")
    names = (*DIMENSIONS[dimension], UNCLASSIFIED)
    buckets: dict[str, list[dict]] = {name: [] for name in names}
    for trade in trades:
        label = labels.get(str(trade["trade_date"])[:10]) or {}
        name = label.get(dimension)
        buckets[name if name in buckets and name != UNCLASSIFIED else UNCLASSIFIED].append(trade)
    return [_state_row(name, buckets[name]) for name in names]


def unclassified_summary(trades: list[dict], labels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """How many trades lack a trend or a volatility label, by reason code."""
    summary: dict[str, Any] = {"trend": {"trades": 0, "reasons": {}}}
    summary["volatility"] = {"trades": 0, "reasons": {}}
    for trade in trades:
        label = labels.get(str(trade["trade_date"])[:10]) or _unclassified(REASON_BAD_DATE)
        for dimension in DIMENSIONS:
            if label.get(dimension) is None:
                block = summary[dimension]
                block["trades"] += 1
                reason = label.get("reason") or REASON_INSUFFICIENT_HISTORY
                block["reasons"][reason] = block["reasons"].get(reason, 0) + 1
    return summary
