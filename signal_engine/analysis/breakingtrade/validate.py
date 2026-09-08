"""Does a scan firing actually precede a move? MFE/MAE forward test.

THE QUESTION THIS ANSWERS

A scan says a symbol is "in play". That is a claim about the FUTURE - cheap to believe and
expensive to assume. This module tests it the only honest way available before risking money:
take every scan hit already recorded in breakingtrade.db, look at what the symbol did
afterwards, and measure it.

MFE and MAE (Maximum Favourable / Adverse Excursion, from Sweeney's trade analysis work):

    MFE = how far price went IN YOUR FAVOUR before the horizon expired
    MAE = how far it went AGAINST you

Both measured from the price at the moment the scan fired, in the scan's own direction (for a
"down" scan, favourable means down). Reported in R, where 1R = one ATR at signal time, so a
Rs 87 bank and a Rs 11,000 holding company are comparable.

HOW TO READ THE RESULT

  MFE >> MAE   the scan found direction. There is something to trade.
  MFE ~= MAE   THE COMMON OUTCOME, AND THE ONE TO WATCH FOR. The scan found VOLATILITY, not
               direction. It will FEEL predictive - big moves keep showing up - while being
               worthless directionally, because price moves against you exactly as far as it
               moves for you. Any apparent profit is then entry-timing luck.
  MFE << MAE   the scan is inverted; the fade is the trade.

The random control matters as much as the numbers. F&O names are volatile intraday, so ANY
selection shows respectable MFE. The scan only has edge if it beats a random pick from the same
universe at the same timestamps - that comparison is what separates a real signal from "we
selected liquid stocks during market hours".

WHAT THIS CANNOT TELL YOU: it measures the SELECTION, not a strategy. It assumes entry at the
signal price with no slippage and no commission. Real costs on this book run about 0.19R per
round trip (see PRD.md), so a scan whose edge is under ~0.2R is not tradeable even if the
statistics look positive.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import httpx
import pandas as pd

from signal_engine.analysis.breakingtrade import store

# One ATR at signal time is 1R. 14 periods on 5-minute bars is the usual intraday setting.
ATR_PERIOD = 14
BAR_INTERVAL = "5m"
DEFAULT_HORIZONS_MIN = (30, 60)


class HistoryUnavailable(RuntimeError):
    """Raised when OpenAlgo's history endpoint cannot be reached."""


def fetch_bars(symbol: str, day: datetime, exchange: str = "NSE") -> pd.DataFrame:
    """Intraday bars for one symbol on one day, via OpenAlgo's own history endpoint."""
    from signal_engine.config import settings

    payload = {
        "apikey": settings.openalgo_api_key,
        "symbol": symbol,
        "exchange": exchange,
        "interval": BAR_INTERVAL,
        "start_date": day.strftime("%Y-%m-%d"),
        "end_date": day.strftime("%Y-%m-%d"),
    }
    url = f"{settings.openalgo_base_url}/api/v1/history"
    try:
        response = httpx.post(url, json=payload, timeout=30)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - any transport failure means "no data"
        raise HistoryUnavailable(f"{symbol}: {type(exc).__name__}: {exc}") from exc

    rows = body.get("data") or []
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    if "timestamp" in frame.columns:
        # OpenAlgo's history endpoint returns UNIX epoch seconds - a UTC instant. Every caller
        # of fetch_bars (initial_balance's IB_START/IB_END window, entry_trigger's signal_time
        # comparison, this module's own captured_at) is written in NAIVE IST, on the assumption
        # bars already were too. They were not: a bar genuinely stamped 09:15 IST arrived here
        # as 03:45 (09:15 minus the 5:30 offset), so the 09:15-10:15 Initial Balance window
        # never matched a single row, initial_balance() returned (None, None) for every symbol
        # on every day, and plan_trade() returned None before it ever reached the entry trigger
        # check - the reason NO BreakingTrade signal has ever fired despite dozens of watchlist
        # calls. Shifted here, once, rather than at each naive-IST comparison site.
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"], unit="s", errors="coerce"
        ) + pd.Timedelta(hours=5, minutes=30)
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return frame


def average_true_range(bars: pd.DataFrame, period: int = ATR_PERIOD) -> float | None:
    """Wilder-style ATR over the bars available before the signal."""
    if len(bars) < 2:
        return None
    high, low, close = bars["high"], bars["low"], bars["close"]
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    value = float(true_range.tail(period).mean())
    return value if value > 0 else None


def _first_touch(bars: pd.DataFrame, entry: float, direction: str, r_value: float) -> str | None:
    """Whether +1R or -1R came first - the question a real stop/target pair asks.

    Resolved bar by bar. When a single bar touches both, the LOSS is assumed first: a bar's
    internal path is unknowable from OHLC, and the alternative flatters every result.
    """
    target = entry + r_value if direction == "up" else entry - r_value
    stop = entry - r_value if direction == "up" else entry + r_value

    for bar in bars.itertuples():
        if direction == "up":
            hit_stop, hit_target = bar.low <= stop, bar.high >= target
        else:
            hit_stop, hit_target = bar.high >= stop, bar.low <= target
        if hit_stop:
            return "-1R"
        if hit_target:
            return "+1R"
    return None


def excursions(bars: pd.DataFrame, entry: float, direction: str, r_value: float) -> dict:
    """MFE / MAE in R over the given bars, measured from `entry` in `direction`."""
    if bars.empty or not r_value:
        return {"mfe_r": None, "mae_r": None, "outcome": None}

    highest, lowest = float(bars["high"].max()), float(bars["low"].min())
    if direction == "up":
        mfe, mae = highest - entry, entry - lowest
    else:
        mfe, mae = entry - lowest, highest - entry

    return {
        "mfe_r": mfe / r_value,
        "mae_r": mae / r_value,
        "outcome": _first_touch(bars, entry, direction, r_value),
    }


def evaluate_hit(
    symbol: str, fired_at: datetime, direction: str, horizons=DEFAULT_HORIZONS_MIN
) -> dict | None:
    """Measure one scan hit. None when the symbol has no usable bars that day."""
    bars = fetch_bars(symbol, fired_at)
    if bars.empty:
        return None

    before = bars[bars["timestamp"] <= fired_at]
    after = bars[bars["timestamp"] > fired_at]
    if before.empty or after.empty:
        return None

    entry = float(before.iloc[-1]["close"])
    r_value = average_true_range(before)
    if not r_value:
        return None

    record = {"symbol": symbol, "fired_at": fired_at, "direction": direction, "r_value": r_value}
    for minutes in horizons:
        window = after[after["timestamp"] <= fired_at + timedelta(minutes=minutes)]
        measured = excursions(window, entry, direction, r_value)
        record[f"mfe_r_{minutes}"] = measured["mfe_r"]
        record[f"mae_r_{minutes}"] = measured["mae_r"]
        record[f"outcome_{minutes}"] = measured["outcome"]
    return record


def load_hits(since: datetime = None, only_new: bool = True) -> pd.DataFrame:
    """Scan hits recorded by the poller.

    only_new keeps just the poll where a symbol ENTERED the scan. A symbol that keeps matching
    for two hours would otherwise contribute a dozen overlapping, near-identical observations,
    making one move look like a dozen successes.
    """
    query = "SELECT captured_at, scan, direction, symbol, is_new FROM scan_hits"
    clauses, params = [], []
    if only_new:
        clauses.append("is_new = 1")
    if since:
        clauses.append("captured_at >= ?")
        params.append(since.isoformat(sep=" "))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    with sqlite3.connect(store._DB_PATH) as conn:
        hits = pd.read_sql_query(query, conn, params=params)
    if not hits.empty:
        hits["captured_at"] = pd.to_datetime(hits["captured_at"])
    return hits


def control_sample(hits: pd.DataFrame, universe: list, seed: int = 7) -> pd.DataFrame:
    """The same number of picks at the same timestamps, chosen at random.

    Without this the results are unreadable: F&O names move intraday whatever you do, so a
    respectable MFE proves nothing on its own. Only the gap over the control is edge.
    """
    import random

    rng = random.Random(seed)
    rows = [
        {
            "captured_at": hit.captured_at,
            "scan": "RANDOM CONTROL",
            "direction": hit.direction,
            "symbol": rng.choice(universe),
            "is_new": 1,
        }
        for hit in hits.itertuples()
    ]
    return pd.DataFrame(rows)


def summarize(measured: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Per-scan medians, the MFE/MAE ratio, and the +1R-before--1R rate."""
    mfe, mae, outcome = f"mfe_r_{horizon}", f"mae_r_{horizon}", f"outcome_{horizon}"

    def _aggregate(group):
        resolved = group[group[outcome].notna()]
        wins = int((resolved[outcome] == "+1R").sum())
        median_mae = group[mae].median()
        return pd.Series(
            {
                "n": len(group),
                "median_mfe_r": round(group[mfe].median(), 2),
                "median_mae_r": round(median_mae, 2),
                "mfe_mae_ratio": round(group[mfe].median() / median_mae, 2) if median_mae else None,
                "resolved": len(resolved),
                "win_rate_1r": round(wins / len(resolved), 2) if len(resolved) else None,
            }
        )

    return measured.groupby("scan", group_keys=False).apply(_aggregate).reset_index()


def run(since: datetime = None, horizons=DEFAULT_HORIZONS_MIN, with_control: bool = True):
    """Measure every recorded scan hit and print the per-scan summary."""
    hits = load_hits(since)
    if hits.empty:
        print(
            "No scan transitions recorded yet. Run the poller (--watch) across a few sessions "
            "first - this measures what was captured live, and cannot be back-filled, because "
            "the scanner publishes no history of its own."
        )
        return None

    if with_control:
        universe = sorted(hits["symbol"].unique())
        hits = pd.concat([hits, control_sample(hits, universe)], ignore_index=True)

    measured, skipped = [], 0
    for hit in hits.itertuples():
        try:
            record = evaluate_hit(
                hit.symbol, hit.captured_at.to_pydatetime(), hit.direction, horizons
            )
        except HistoryUnavailable as exc:
            print(f"  history unavailable for {hit.symbol}: {exc}")
            skipped += 1
            continue
        if record is None:
            skipped += 1
            continue
        record["scan"] = hit.scan
        measured.append(record)

    if not measured:
        print(f"No hit could be measured ({skipped} skipped - is OpenAlgo running?).")
        return None

    frame = pd.DataFrame(measured)
    print(f"measured {len(frame)} hits, skipped {skipped}")
    for horizon in horizons:
        print(f"\n=== {horizon} minutes after the signal ===")
        print(summarize(frame, horizon).to_string(index=False))
    print(
        "\nRead MFE against MAE, never MFE alone. If they are close, the scan selected "
        "volatility rather than direction. Costs run ~0.19R per round trip, so an edge under "
        "that is not tradeable. Compare every scan against RANDOM CONTROL."
    )
    return frame
