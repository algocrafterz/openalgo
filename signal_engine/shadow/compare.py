"""Compare recorded WebSocket candles with Flattrade history for one or more sessions.

Run after the close, once per session (it makes one history call per symbol and day):

    uv run python -m signal_engine.shadow.compare --dates 2026-10-30 2026-10-31

Acceptance criteria are the constants below. The tick-built bar replaces the history bar only
if every criterion holds in every session of a run of at least MIN_SESSIONS sessions.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

import httpx
import pandas as pd

from signal_engine.pipeline.bars import parse_history
from signal_engine.shadow import store
from signal_engine.shadow.tick_bars import build_bars
from signal_engine.timeutils import IST

# Acceptance criteria. A session passes only when every one holds.
MIN_SESSIONS = 5
COVERAGE_MIN = 0.99  # share of history bars (outside feed gaps) that have a tick-built bar
EXTRA_BARS_MAX = 0  # tick-built bars at times history has no bar
CLOSE_MATCH_MIN = 0.99  # share of bars whose close is within tolerance
OPEN_MATCH_MIN = 0.95
EXTREME_MATCH_MIN = 0.95  # high_aug and low_aug, each
SPURIOUS_EXTREME_MAX = 0.005  # share of bars where the tick range exceeds the history range
VOLUME_MEDIAN_ERR_MAX = 0.01
VOLUME_P95_ERR_MAX = 0.05
ARRIVAL_LAG_P99_MAX_S = 1.0
SOURCE_DISAGREE_MAX = 0.01  # arrival vs exchange time assignment giving different closes

SESSION_FIRST_BAR = "09:15"
SESSION_LAST_BAR = "15:25"


@dataclass(frozen=True)
class Criterion:
    name: str
    value: float
    limit: float
    higher_is_better: bool

    @property
    def passed(self) -> bool:
        if pd.isna(self.value):
            return False
        return self.value >= self.limit if self.higher_is_better else self.value <= self.limit


def session_slice(hist: pd.DataFrame) -> pd.DataFrame:
    return hist.between_time(SESSION_FIRST_BAR, SESSION_LAST_BAR)


def in_gap(index: pd.DatetimeIndex, gaps, interval_min: int = 5) -> pd.Series:
    """True for bars whose [open, close) overlaps a recorded feed disconnect."""
    flags = pd.Series(False, index=index)
    width = pd.Timedelta(minutes=interval_min)
    for start, end in gaps:
        flags |= (index < end) & (index + width > start)
    return flags


def compare_symbol(ticks, hist: pd.DataFrame, gaps, tol: float) -> pd.DataFrame:
    """One row per history bar with the differences against the tick-built bar."""
    hist = session_slice(hist)
    arrival = build_bars(ticks, "arrival").reindex(hist.index)
    exchange = build_bars(ticks, "exchange").reindex(hist.index)
    rows = pd.DataFrame(index=hist.index)
    rows["has_ticks"] = arrival["ticks"].fillna(0) > 0
    rows["in_gap"] = in_gap(hist.index, gaps)
    rows["ticks"] = arrival["ticks"].fillna(0)
    for field, src in (
        ("open", "open"),
        ("close", "close"),
        ("high", "high_aug"),
        ("low", "low_aug"),
    ):
        rows[f"{field}_err"] = (arrival[src] - hist[field]).abs()
        rows[f"{field}_ok"] = rows[f"{field}_err"] <= tol
    rows["high_plain_ok"] = (arrival["high"] - hist["high"]).abs() <= tol
    rows["low_plain_ok"] = (arrival["low"] - hist["low"]).abs() <= tol
    rows["spurious"] = (arrival["high"] > hist["high"] + tol) | (arrival["low"] < hist["low"] - tol)
    rows["volume_err"] = ((arrival["volume"] - hist["volume"]).abs() / hist["volume"]).where(
        hist["volume"] > 0
    )
    rows["source_disagree"] = (arrival["close"] - exchange["close"]).abs() > tol
    extra = build_bars(ticks, "arrival")
    extra = extra[(extra["ticks"] > 0)]
    rows.attrs["extra_bars"] = int((~extra.index.isin(hist.index)).sum())
    rows.attrs["first_bar"] = hist.index[0].strftime("%H:%M") if len(hist) else None
    return rows


def arrival_lags(ticks_by_symbol) -> pd.Series:
    lags = [
        (t.arrival - t.exchange_time).total_seconds()
        for ticks in ticks_by_symbol.values()
        for t in ticks
        if t.exchange_time is not None
    ]
    return pd.Series(lags, dtype="float64")


def evaluate(per_symbol: dict[str, pd.DataFrame], lags: pd.Series) -> list[Criterion]:
    """Session-level criteria from the per-symbol comparison frames."""
    if not per_symbol:
        return []
    allrows = pd.concat(per_symbol.values())
    scored = allrows[~allrows["in_gap"]]
    covered = scored[scored["has_ticks"]]
    n = max(len(scored), 1)
    m = max(len(covered), 1)
    extra = sum(f.attrs.get("extra_bars", 0) for f in per_symbol.values())
    volume = covered["volume_err"].dropna()
    return [
        Criterion("coverage", len(covered) / n, COVERAGE_MIN, True),
        Criterion("extra_bars", float(extra), EXTRA_BARS_MAX, False),
        Criterion("close_match", covered["close_ok"].sum() / m, CLOSE_MATCH_MIN, True),
        Criterion("open_match", covered["open_ok"].sum() / m, OPEN_MATCH_MIN, True),
        Criterion("high_match", covered["high_ok"].sum() / m, EXTREME_MATCH_MIN, True),
        Criterion("low_match", covered["low_ok"].sum() / m, EXTREME_MATCH_MIN, True),
        Criterion("spurious_extreme", covered["spurious"].sum() / m, SPURIOUS_EXTREME_MAX, False),
        Criterion(
            "volume_median_err",
            volume.median() if len(volume) else float("nan"),
            VOLUME_MEDIAN_ERR_MAX,
            False,
        ),
        Criterion(
            "volume_p95_err",
            volume.quantile(0.95) if len(volume) else float("nan"),
            VOLUME_P95_ERR_MAX,
            False,
        ),
        Criterion(
            "arrival_lag_p99_s",
            lags.quantile(0.99) if len(lags) else float("nan"),
            ARRIVAL_LAG_P99_MAX_S,
            False,
        ),
        Criterion(
            "source_disagree", covered["source_disagree"].sum() / m, SOURCE_DISAGREE_MAX, False
        ),
    ]


def fetch_history(
    base_url: str, api_key: str, symbol: str, day: str, exchange: str
) -> pd.DataFrame:
    payload = {
        "apikey": api_key,
        "symbol": symbol,
        "exchange": exchange,
        "interval": "5m",
        "start_date": day,
        "end_date": day,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{base_url}/api/v1/history", json=payload)
        resp.raise_for_status()
        rows = resp.json().get("data") or []
    if not rows:
        return pd.DataFrame()
    return parse_history(rows).rename(columns=str.lower)


def run_session(day: str, base_url: str, api_key: str, exchange: str, tol: float, pause_s: float):
    path = store.db_path(day)
    if not path.exists():
        raise FileNotFoundError(f"no recorded ticks for {day}: {path}")
    conn = store.connect(path)
    try:
        ticks = store.load_ticks(conn)
        gaps = store.load_gaps(conn)
    finally:
        conn.close()
    per_symbol = {}
    for symbol, symbol_ticks in sorted(ticks.items()):
        hist = fetch_history(base_url, api_key, symbol, day, exchange)
        time.sleep(pause_s)
        if hist.empty:
            continue
        per_symbol[symbol] = compare_symbol(symbol_ticks, hist, gaps, tol)
    return per_symbol, arrival_lags(ticks), gaps


def render(day: str, criteria: list[Criterion], per_symbol, gaps) -> str:
    verdict = "PASS" if criteria and all(c.passed for c in criteria) else "FAIL"
    lines = [
        f"## {day}: {verdict}",
        "",
        "| criterion | value | limit | result |",
        "|---|---|---|---|",
    ]
    for c in criteria:
        op = ">=" if c.higher_is_better else "<="
        lines.append(
            f"| {c.name} | {c.value:.4f} | {op} {c.limit} | {'ok' if c.passed else 'FAIL'} |"
        )
    lines.append("")
    lines.append(f"symbols compared: {len(per_symbol)}; feed gaps: {len(gaps)}")
    worst = sorted(
        (
            (s, 1 - f[~f["in_gap"] & f["has_ticks"]]["close_ok"].mean())
            for s, f in per_symbol.items()
        ),
        key=lambda x: -x[1],
    )[:5]
    lines.append(
        "worst symbols by close mismatch: " + ", ".join(f"{s} {v:.1%}" for s, v in worst if v == v)
    )
    return "\n".join(lines)


def main() -> None:
    from signal_engine.config import settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument(
        "--tolerance", type=float, default=0.05, help="price difference counted as a match"
    )
    parser.add_argument("--pause", type=float, default=0.3, help="seconds between history calls")
    args = parser.parse_args()

    sections, passed = [], 0
    for day in args.dates:
        per_symbol, lags, gaps = run_session(
            day,
            settings.openalgo_base_url,
            settings.openalgo_api_key,
            args.exchange,
            args.tolerance,
            args.pause,
        )
        criteria = evaluate(per_symbol, lags)
        passed += bool(criteria) and all(c.passed for c in criteria)
        sections.append(render(day, criteria, per_symbol, gaps))
    enough = len(args.dates) >= MIN_SESSIONS
    overall = "ACCEPT" if enough and passed == len(args.dates) else "NOT ACCEPTED"
    print(
        f"# Shadow candle comparison: {overall} ({passed}/{len(args.dates)} sessions pass, "
        f"{MIN_SESSIONS} needed)\n"
    )
    print("\n\n".join(sections))


if __name__ == "__main__":
    main()
