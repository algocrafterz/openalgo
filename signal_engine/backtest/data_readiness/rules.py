"""Data-readiness rules over a Historify-shaped `market_data` relation.

Each rule has an id, a version, one data dimension, one action and a written rationale, and
returns symbol/session evidence. Rules never repair data and never turn a SQL error into a pass:
DuckDB errors propagate. All session dates are IST. Thresholds come from the versioned policy
(`policy.py`), never from literals here.

Plan: signal_engine/docs/BACKTEST-DATA-QUALITY-IMPLEMENTATION-PLAN.md, section 5.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from signal_engine.backtest.data import _DUCKDB_CONFIG, SESSION_CLOSE, SESSION_OPEN
from signal_engine.backtest.data_readiness.policy import Policy
from signal_engine.backtest.eligibility import LISTING_TOLERANCE_SESSIONS


class Action(Enum):
    BLOCK = "BLOCK"
    EXCLUDE_SYMBOL = "EXCLUDE_SYMBOL"
    EXCLUDE_SESSION = "EXCLUDE_SESSION"
    WARNING = "WARNING"
    INFO = "INFO"


DIMENSIONS = (
    "validity", "completeness", "identity_continuity", "coverage", "calendar",
    "universe_membership", "survivorship", "adjustment", "snapshot_integrity", "freshness",
)
INTERVALS = ("D", "1m")
EVALUATED = "evaluated"
NOT_EVALUATED = "not_evaluated"
EVIDENCE_COLUMNS = ["symbol", "session", "rows", "detail"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: IST is a fixed UTC+05:30 offset with no daylight saving, so the IST session date and minute of
#: day come from integer arithmetic on the epoch seconds. This avoids a time-zone conversion per
#: row, which matters on the 165M-row 1m table.
_IST_OFFSET_S = 19800
_SESSION = f"(DATE '1970-01-01' + CAST((timestamp + {_IST_OFFSET_S}) // 86400 AS INTEGER))"
_MINUTE = f"CAST(((timestamp + {_IST_OFFSET_S}) % 86400) // 60 AS INTEGER)"


def _hhmm_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


_DAILY_STAMP_MIN = _hhmm_minutes("05:30")
_OPEN_MIN, _CLOSE_MIN = _hhmm_minutes(SESSION_OPEN), _hhmm_minutes(SESSION_CLOSE)


@dataclass(frozen=True)
class Scope:
    """Which rows a rule looks at: one interval, optionally some symbols and an IST date range."""

    interval: str
    symbols: tuple[str, ...] | None = None
    start: date | None = None
    end: date | None = None
    table: str = "market_data"

    def __post_init__(self) -> None:
        if self.interval not in INTERVALS:
            raise ValueError(f"interval must be one of {INTERVALS}, got {self.interval!r}")
        if not _IDENT.fullmatch(self.table):
            raise ValueError(f"invalid table name {self.table!r}")


@dataclass(frozen=True)
class Context:
    """Inputs a rule may need besides the data: the policy and the session calendar."""

    policy: Policy
    calendar: tuple[date, ...] | None = None


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    version: int
    dimension: str
    action: Action
    status: str
    rows: int
    evidence: pd.DataFrame = field(repr=False)
    reason: str = ""

    @property
    def symbols(self) -> dict[str, int]:
        """Offending row count per symbol (date-level rows without a symbol are left out)."""
        ev = self.evidence.dropna(subset=["symbol"])
        return {str(k): int(v) for k, v in ev.groupby("symbol")["rows"].sum().items()}

    @property
    def sessions(self) -> list[str]:
        ev = self.evidence.dropna(subset=["session"])
        return sorted({str(d) for d in ev["session"]})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    dimension: str
    action: Action
    intervals: frozenset[str]
    rationale: str
    run: Callable[[duckdb.DuckDBPyConnection, Scope, Context], tuple[pd.DataFrame, int]]
    needs_calendar: bool = False

    def applies_to(self, interval: str) -> bool:
        return interval in self.intervals


@contextmanager
def connect_readonly(path: Path | str) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only DuckDB connection with the project's memory cap, always closed."""
    con = duckdb.connect(str(path), read_only=True, config=_DUCKDB_CONFIG)
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------- query helpers


def _bars(scope: Scope, extra: str = "", in_session: bool = False) -> tuple[str, list[Any]]:
    """The `bars` CTE for the scope, with IST session date and minute of day, plus parameters.

    `in_session` keeps only 1m bars inside the continuous session (the loader drops the rest), so
    an out-of-hours artefact cannot count as a defect. Rows with a NULL timestamp are kept so the
    null-row rule still sees them.
    """
    where = ["interval = ?"]
    params: list[Any] = [scope.interval]
    if scope.symbols is not None:
        where.append("list_contains(?::VARCHAR[], symbol)")
        params.append(list(scope.symbols))
    if scope.start is not None:
        where.append(f"{_SESSION} >= ?")
        params.append(scope.start)
    if scope.end is not None:
        where.append(f"{_SESSION} <= ?")
        params.append(scope.end)
    if in_session and scope.interval == "1m":
        where.append(f"(timestamp IS NULL OR {_MINUTE} BETWEEN {_OPEN_MIN} AND {_CLOSE_MIN})")
    cols = "symbol, exchange, timestamp, open, high, low, close, volume" + extra
    sql = (f"bars AS (SELECT {cols}, {_MINUTE} AS ist_min, {_SESSION} AS session "
           f"FROM {scope.table} WHERE {' AND '.join(where)})")
    return sql, params


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise evidence to the standard columns and order."""
    out = df.copy()
    for col in EVIDENCE_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[EVIDENCE_COLUMNS]
    out["rows"] = out["rows"].astype("int64")
    if len(out):
        out["session"] = pd.to_datetime(out["session"]).dt.date
    return out.sort_values(["symbol", "session"], na_position="first").reset_index(drop=True)


def _empty() -> pd.DataFrame:
    return _frame(pd.DataFrame({c: [] for c in EVIDENCE_COLUMNS}))


def _per_session(con, scope: Scope, condition: str,
                 in_session: bool = False) -> tuple[pd.DataFrame, int]:
    bars, params = _bars(scope, in_session=in_session)
    df = con.execute(
        f"WITH {bars} SELECT symbol, session, COUNT(*) AS rows FROM bars "
        f"WHERE {condition} GROUP BY 1, 2", params).df()
    ev = _frame(df)
    return ev, int(ev["rows"].sum())


def _columns(con, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {r[1] for r in rows}


def _calendar_list(ctx: Context) -> list[date]:
    assert ctx.calendar is not None
    return sorted(ctx.calendar)


# ---------------------------------------------------------------------------- rule bodies


def _dup_key(con, scope, ctx):
    bars, params = _bars(scope)
    if scope.interval == "D":
        sql = (f"WITH {bars} SELECT symbol, session, COUNT(*) - 1 AS rows FROM bars "
               "GROUP BY symbol, session HAVING COUNT(*) > 1")
    else:
        sql = (f"WITH {bars}, d AS (SELECT symbol, session, COUNT(*) - 1 AS extra FROM bars "
               "GROUP BY symbol, session, timestamp HAVING COUNT(*) > 1) "
               "SELECT symbol, session, SUM(extra) AS rows FROM d GROUP BY 1, 2")
    ev = _frame(con.execute(sql, params).df())
    return ev, int(ev["rows"].sum())


def _daily_ts(con, scope, ctx):
    return _per_session(con, scope, f"ist_min <> {_DAILY_STAMP_MIN}")


def _exchange(con, scope, ctx):
    bars, params = _bars(scope)
    df = con.execute(
        f"WITH {bars} SELECT symbol, COUNT(DISTINCT exchange) - 1 AS rows, "
        "string_agg(DISTINCT exchange, ',') AS detail FROM bars GROUP BY symbol "
        "HAVING COUNT(DISTINCT exchange) > 1", params).df()
    ev = _frame(df)
    return ev, int(ev["rows"].sum())


_OHLC_BAD = "(high < greatest(open, close) OR low > least(open, close) OR high < low)"
_NONPOS = "(least(open, high, low, close) <= 0)"
_NEG_VOL = "(volume < 0)"
#: Null keys or values, or a non-finite price or volume. DuckDB orders NaN above every number,
#: so a NaN high passes the OHLC comparisons; it has to be caught here.
_NULL_ROW = ("(symbol IS NULL OR timestamp IS NULL OR open IS NULL OR high IS NULL "
             "OR low IS NULL OR close IS NULL OR volume IS NULL "
             "OR NOT isfinite(open) OR NOT isfinite(high) OR NOT isfinite(low) "
             "OR NOT isfinite(close) OR NOT isfinite(CAST(volume AS DOUBLE)))")
_OUTSIDE = f"(ist_min < {_OPEN_MIN} OR ist_min > {_CLOSE_MIN})"


def _locked(con, scope, ctx):
    return _per_session(con, scope, "high = low AND volume > 0")


def _placeholder(con, scope, ctx):
    p = ctx.policy
    n = int(p.placeholder_lookback_sessions)
    bars, params = _bars(scope)
    # Tie-break on the values so duplicate timestamps order deterministically.
    frame = (f"PARTITION BY symbol ORDER BY timestamp, open, high, low, close, volume "
             f"ROWS BETWEEN {n} PRECEDING AND 1 PRECEDING")
    sql = (f"WITH {bars}, w AS (SELECT symbol, session, high, low, volume, "
           f"median(volume) OVER ({frame}) AS med, count(volume) OVER ({frame}) AS n FROM bars) "
           "SELECT symbol, session, COUNT(*) AS rows, "
           "string_agg(CAST(volume AS VARCHAR) || ' vs median ' || CAST(med AS VARCHAR), '; ') "
           "AS detail FROM w WHERE high = low AND n >= 1 AND med > 0 AND volume < ? * med "
           "GROUP BY 1, 2")
    ev = _frame(con.execute(sql, [*params, p.placeholder_volume_ratio]).df())
    return ev, int(ev["rows"].sum())


def _unexpected(con, scope, ctx) -> pd.DataFrame:
    """Per off-calendar date: how many symbols have bars, and how many were active then."""
    bars, params = _bars(scope)
    sql = (f"WITH {bars}, spans AS (SELECT symbol, MIN(session) AS first, MAX(session) AS last "
           "FROM bars GROUP BY symbol), "
           "off AS (SELECT DISTINCT symbol, session FROM bars "
           "WHERE session IS NOT NULL AND NOT list_contains(?::DATE[], session)), "
           "act AS (SELECT d.session, COUNT(*) AS active FROM (SELECT DISTINCT session FROM off) d "
           "JOIN spans s ON s.first <= d.session AND s.last >= d.session GROUP BY d.session) "
           "SELECT o.symbol, o.session, COUNT(*) OVER (PARTITION BY o.session) AS on_date, "
           "a.active FROM off o JOIN act a ON a.session = o.session")
    return con.execute(sql, [*params, _calendar_list(ctx)]).df()


def _unexpected_date(con, scope, ctx):
    df = _unexpected(con, scope, ctx)
    if df.empty:
        return _empty(), 0
    df["share"] = df["on_date"] / df["active"]
    wide = df[df["share"] >= ctx.policy.calendar_date_symbol_share]
    per_date = (wide.groupby("session")
                .agg(rows=("symbol", "count"), active=("active", "first")).reset_index())
    per_date["symbol"] = None
    per_date["detail"] = [f"{r} of {a} active symbols" for r, a in
                          zip(per_date["rows"], per_date["active"], strict=True)]
    ev = _frame(per_date)
    return ev, len(ev)


def _unexpected_isolated(con, scope, ctx):
    df = _unexpected(con, scope, ctx)
    if df.empty:
        return _empty(), 0
    df["share"] = df["on_date"] / df["active"]
    few = df[df["share"] < ctx.policy.calendar_date_symbol_share].copy()
    few["rows"] = 1
    ev = _frame(few)
    return ev, len(ev)


def _missing(con, scope, ctx) -> pd.DataFrame:
    """Per symbol: calendar sessions inside its first-to-last range with no bar."""
    bars, params = _bars(scope)
    have = con.execute(f"WITH {bars} SELECT DISTINCT symbol, session FROM bars", params).df()
    cal = sorted(_calendar_list(ctx))
    out = []
    for sym, grp in have.groupby("symbol"):
        present = set(pd.to_datetime(grp["session"]).dt.date)
        first, last = min(present), max(present)
        expected = [d for d in cal if first <= d <= last]
        missing = [d for d in expected if d not in present]
        out.append((sym, len(expected), missing))
    return pd.DataFrame(out, columns=["symbol", "expected", "missing"])


def _missing_many(con, scope, ctx):
    df = _missing(con, scope, ctx)
    limit = ctx.policy.max_missing_session_share
    rows = []
    for sym, expected, missing in df.itertuples(index=False):
        share = len(missing) / expected if expected else 0.0
        if share > limit:
            rows.append({"symbol": sym, "session": None, "rows": len(missing),
                         "detail": f"{len(missing)} of {expected} sessions missing"})
    ev = _frame(pd.DataFrame(rows)) if rows else _empty()
    return ev, int(ev["rows"].sum())


def _missing_few(con, scope, ctx):
    df = _missing(con, scope, ctx)
    limit = ctx.policy.max_missing_session_share
    rows = []
    for sym, expected, missing in df.itertuples(index=False):
        share = len(missing) / expected if expected else 0.0
        if 0 < share <= limit:
            rows += [{"symbol": sym, "session": d, "rows": 1, "detail": "missing"}
                     for d in missing]
    ev = _frame(pd.DataFrame(rows)) if rows else _empty()
    return ev, int(ev["rows"].sum())


def _latest_incomplete(con, scope, ctx):
    p = ctx.policy
    if scope.interval == "D":
        # created_at is naive IST wall-clock time. Verified on the real store 2026-10-04: SBIN's
        # last 1m bar of 2026-10-01 (15:29 IST) has created_at 15:39, after the 15:35 IST
        # backfill cron started; a UTC value would read 10:09.
        bars, params = _bars(scope, extra=", created_at")
        sql = (f"WITH {bars}, last AS (SELECT symbol, session, created_at FROM bars "
               "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY timestamp DESC) = 1) "
               "SELECT symbol, session, 1 AS rows, COALESCE('written ' || "
               "strftime(created_at, '%Y-%m-%d %H:%M'), 'write time unknown') AS detail "
               "FROM last WHERE created_at IS NULL OR strftime(created_at, '%Y-%m-%d %H:%M') < "
               "strftime(session, '%Y-%m-%d') || ' ' || ?")
        ev = _frame(con.execute(sql, [*params, p.daily_close_time]).df())
        return ev, int(ev["rows"].sum())
    bars, params = _bars(scope)
    counts = con.execute(
        f"WITH {bars} SELECT symbol, session, COUNT(*) AS n FROM bars "
        f"WHERE NOT {_OUTSIDE} GROUP BY 1, 2", params).df()
    # Reference: the larger of the symbol's own modal count (when it has history) and the
    # median count of all symbols on that session, so a single-session symbol is still checked.
    across = counts.groupby("session")["n"].median()
    rows = []
    for sym, grp in counts.groupby("symbol"):
        grp = grp.sort_values("session")
        last = grp.iloc[-1]
        own = int(grp["n"].mode().max()) if len(grp) >= 2 else 0
        ref = max(own, float(across[last["session"]]))
        if last["n"] < p.latest_session_min_bar_share * ref:
            rows.append({"symbol": sym, "session": last["session"], "rows": 1,
                         "detail": f"{int(last['n'])} bars against a reference {ref:g}"})
    ev = _frame(pd.DataFrame(rows)) if rows else _empty()
    return ev, int(ev["rows"].sum())


def _zero_volume(con, scope, ctx):
    bars, params = _bars(scope, in_session=True)
    df = con.execute(
        f"WITH {bars} SELECT symbol, SUM(CASE WHEN volume = 0 THEN 1 ELSE 0 END) AS rows, "
        "COUNT(*) AS total FROM bars GROUP BY symbol", params).df()
    df = df[df["rows"] > ctx.policy.zero_volume_share * df["total"]].copy()
    df["detail"] = [f"{r} of {t} bars" for r, t in zip(df["rows"], df["total"], strict=True)]
    ev = _frame(df)
    return ev, int(ev["rows"].sum())


def _daily_frame(con, scope) -> pd.DataFrame:
    bars, params = _bars(scope)
    return con.execute(
        f"WITH {bars} SELECT symbol, session, open, high, low, close, volume FROM bars "
        "ORDER BY symbol, timestamp", params).df()


def _spike(con, scope, ctx):
    df = _daily_frame(con, scope)
    x = ctx.policy.spike_return
    rows = []
    for sym, g in df.groupby("symbol", sort=False):
        c = g["close"].to_numpy(dtype=float)
        for i in range(1, len(c) - 1):
            move, back = c[i] - c[i - 1], c[i + 1] - c[i]
            if c[i - 1] > 0 and abs(move) / c[i - 1] > x and move * back < 0 \
                    and abs(back) >= 0.5 * abs(move):
                rows.append({"symbol": sym, "session": g["session"].iloc[i], "rows": 1,
                             "detail": f"{c[i - 1]:g} -> {c[i]:g} -> {c[i + 1]:g}"})
    ev = _frame(pd.DataFrame(rows)) if rows else _empty()
    return ev, int(ev["rows"].sum())


def _stale(con, scope, ctx):
    df = _daily_frame(con, scope)
    n = ctx.policy.stale_repeat_sessions
    rows = []
    for sym, g in df.groupby("symbol", sort=False):
        key = list(zip(g["open"], g["high"], g["low"], g["close"], strict=True))
        zero = (g["volume"] == 0).tolist()
        sessions = g["session"].tolist()
        start = 0
        for i in range(1, len(key) + 1):
            if i < len(key) and zero[i] and zero[i - 1] and key[i] == key[i - 1]:
                continue
            if zero[start] and i - start >= n:
                rows += [{"symbol": sym, "session": s, "rows": 1,
                          "detail": f"run of {i - start}"} for s in sessions[start:i]]
            start = i
    ev = _frame(pd.DataFrame(rows)) if rows else _empty()
    return ev, int(ev["rows"].sum())


def _late_start(con, scope, ctx):
    bars, params = _bars(scope)
    df = con.execute(
        f"WITH {bars} SELECT symbol, MIN(session) AS session FROM bars GROUP BY symbol",
        params).df()
    if df.empty:
        return _empty(), 0
    df["session"] = pd.to_datetime(df["session"]).dt.date
    start = df["session"].min()
    cal = sorted(ctx.calendar) if ctx.calendar else sorted(df["session"])
    within = [d for d in cal if d >= start][: LISTING_TOLERANCE_SESSIONS + 1]
    cutoff = within[-1] if within else start
    late = df[df["session"] > cutoff].copy()
    late["rows"] = 1
    late["detail"] = "first session after the store start"
    ev = _frame(late)
    return ev, len(ev)


def _cross_interval(con, scope, ctx):
    p = ctx.policy
    bars, params = _bars(scope)
    minute = Scope("1m", scope.symbols, scope.start, scope.end, scope.table)
    mbars, mparams = _bars(minute)
    mbars = mbars.replace("bars AS (", "mbars AS (", 1)
    sql = (f"WITH {bars}, {mbars}, m AS (SELECT symbol, session, SUM(volume) AS mvol "
           f"FROM mbars WHERE NOT {_OUTSIDE} GROUP BY 1, 2) "
           "SELECT b.symbol, b.session, 1 AS rows, "
           "'daily ' || CAST(b.volume AS VARCHAR) || ' vs 1m ' || CAST(m.mvol AS VARCHAR) "
           "AS detail FROM bars b JOIN m ON b.symbol = m.symbol AND b.session = m.session "
           "WHERE m.mvol > 0 AND (b.volume < ? * m.mvol OR b.volume > ? * m.mvol)")
    ev = _frame(con.execute(
        sql, [*params, *mparams, p.cross_volume_ratio_low, p.cross_volume_ratio_high]).df())
    return ev, int(ev["rows"].sum())


def _symbol_lag(con, scope, ctx):
    bars, params = _bars(scope)
    df = con.execute(f"WITH {bars} SELECT symbol, MAX(session) AS session FROM bars "
                     "WHERE session IS NOT NULL GROUP BY symbol", params).df()
    if df.empty:
        return _empty(), 0
    df["session"] = pd.to_datetime(df["session"]).dt.date
    cal = sorted(ctx.calendar) if ctx.calendar else sorted(set(df["session"]))
    end = max(cal[-1], df["session"].max())
    rows = []
    for sym, last in zip(df["symbol"], df["session"], strict=True):
        behind = sum(1 for d in cal if last < d <= end)
        if behind:
            rows.append({"symbol": sym, "session": last, "rows": behind,
                         "detail": f"last session {last}, {behind} calendar sessions behind"})
    ev = _frame(pd.DataFrame(rows)) if rows else _empty()
    return ev, len(ev)


def _session_prices(con, scope) -> pd.DataFrame:
    """One row per symbol and session: the first open and the last close, in session order.
    1m bars outside the continuous session are left out, as the loader drops them."""
    bars, params = _bars(scope, in_session=True)
    return con.execute(
        f"WITH {bars}, s AS (SELECT symbol, exchange, session, arg_min(open, timestamp) AS o, "
        "arg_max(close, timestamp) AS c FROM bars WHERE timestamp IS NOT NULL GROUP BY 1, 2, 3) "
        "SELECT symbol, session, o, "
        "LAG(c) OVER (PARTITION BY symbol, exchange ORDER BY session) AS prev_c, "
        "LAG(session) OVER (PARTITION BY symbol, exchange ORDER BY session) AS prev_s "
        "FROM s ORDER BY symbol, session", params).df()


def _event_ratio(ratio: float, ratios: tuple[float, ...], tol: float) -> float | None:
    """The corporate-action factor (or its reciprocal) that `ratio` matches, if any."""
    for k in ratios:
        for f in (k, 1.0 / k):
            if abs(ratio / f - 1.0) <= tol:
                return f
    return None


def _jump(con, scope, ctx):
    p = ctx.policy
    df = _session_prices(con, scope)
    df = df[(df["prev_c"] > 0) & (df["o"] > 0)]
    if ctx.calendar and len(df):
        # Only an overnight gap counts: a move across missing sessions is a completeness
        # finding (CMP-MISSING), not evidence of an unadjusted event.
        cal = sorted(ctx.calendar)
        prev_of = dict(zip(cal[1:], cal[:-1], strict=True))
        sess = pd.to_datetime(df["session"]).dt.date
        prev = pd.to_datetime(df["prev_s"]).dt.date
        df = df[[prev_of.get(a) == b for a, b in zip(sess, prev, strict=True)]]
    ratio = df["o"] / df["prev_c"]
    big = df[(ratio >= 1 + p.jump_min_move) | (ratio <= 1 / (1 + p.jump_min_move))]
    rows = []
    for r in big.itertuples(index=False):
        f = _event_ratio(r.o / r.prev_c, p.jump_ratios, p.jump_ratio_tolerance)
        if f is not None:
            rows.append({"symbol": r.symbol, "session": r.session, "rows": 1,
                         "detail": f"open {r.o:g} after close {r.prev_c:g} (factor {f:.4g})"})
    ev = _frame(pd.DataFrame(rows)) if rows else _empty()
    return ev, int(ev["rows"].sum())


def _cross_basis(con, scope, ctx):
    p = ctx.policy
    daily = Scope("D", scope.symbols, scope.start, scope.end, scope.table)
    dbars, dparams = _bars(daily)
    dbars = dbars.replace("bars AS (", "dbars AS (", 1)
    bars, params = _bars(scope, in_session=True)
    sql = (f"WITH {bars}, {dbars}, m AS (SELECT symbol, exchange, session, "
           "arg_max(close, timestamp) AS mc FROM bars WHERE timestamp IS NOT NULL GROUP BY 1, 2, 3), "
           "j AS (SELECT d.symbol, d.session, d.close / m.mc AS ratio FROM dbars d "
           "JOIN m ON d.symbol = m.symbol AND d.exchange = m.exchange AND d.session = m.session "
           "WHERE m.mc > 0 AND d.close > 0), "
           "bad AS (SELECT * FROM j WHERE ratio > 1 + ? OR ratio < 1 / (1 + ?)) "
           "SELECT symbol, session, 1 AS rows, 'daily/1m close ratio ' || "
           "CAST(round(ratio, 4) AS VARCHAR) AS detail FROM bad WHERE symbol IN "
           "(SELECT symbol FROM bad GROUP BY symbol HAVING COUNT(*) >= ?)")
    tol = p.cross_basis_tolerance
    ev = _frame(con.execute(sql, [*params, *dparams, tol, tol, p.cross_basis_min_sessions]).df())
    return ev, int(ev["rows"].sum())


def _has_interval(con, scope: Scope, interval: str) -> bool:
    """Whether the scope's symbols and dates have any bars in `interval`."""
    other = Scope(interval, scope.symbols, scope.start, scope.end, scope.table)
    bars, params = _bars(other)
    return con.execute(f"WITH {bars} SELECT 1 FROM bars LIMIT 1", params).fetchone() is not None


def _cond(condition: str, in_session: bool = True):
    return lambda con, scope, ctx: _per_session(con, scope, condition, in_session)


# ---------------------------------------------------------------------------- catalogue

_BOTH = frozenset(INTERVALS)
_D = frozenset({"D"})
_M = frozenset({"1m"})

_CATALOGUE = (
    Rule("SNAP-DUP-KEY", 1, "snapshot_integrity", Action.BLOCK, _BOTH,
         "More than one row for one symbol and session (daily) or timestamp (1m). The loader "
         "keeps the last row, which can be a fragment (the SBIN incident).", _dup_key),
    Rule("CANON-DAILY-TS", 1, "snapshot_integrity", Action.BLOCK, _D,
         "A daily bar must be stamped 05:30 IST (midnight UTC of the session date). Any other "
         "stamp is a re-download copy, the source of the SBIN duplicates.", _daily_ts),
    Rule("SNAP-EXCHANGE", 1, "snapshot_integrity", Action.BLOCK, _BOTH,
         "A symbol on more than one exchange in one interval; the loaders do not filter on "
         "exchange, so the two series would be merged.", _exchange),
    Rule("VAL-OHLC", 1, "validity", Action.EXCLUDE_SESSION, _BOTH,
         "High below the open or close, low above them, or high below low: the bar is "
         "impossible and its session cannot be trusted.", _cond(_OHLC_BAD)),
    Rule("VAL-NONPOS", 1, "validity", Action.EXCLUDE_SESSION, _BOTH,
         "A price at or below zero is impossible for an NSE equity.", _cond(_NONPOS)),
    Rule("VAL-NEG-VOL", 1, "validity", Action.EXCLUDE_SESSION, _BOTH,
         "Negative volume is objectively invalid (owner decision D12); never repaired.",
         _cond(_NEG_VOL)),
    Rule("VAL-NULL-ROW", 2, "validity", Action.EXCLUDE_SESSION, _BOTH,
         "A row with a null symbol, timestamp, open, high, low, close or volume, or a NaN or "
         "infinite price or volume, is incomplete or invalid.", _cond(_NULL_ROW)),
    Rule("VAL-PLACEHOLDER", 1, "validity", Action.EXCLUDE_SESSION, _D,
         "High equal to low together with volume far below the symbol's recent median: a "
         "placeholder or mock-session bar (D13 escalation), not a real locked day.",
         _placeholder),
    Rule("DIAG-LOCKED", 1, "validity", Action.WARNING, _D,
         "High equal to low with volume: possibly a circuit lock or an illiquid day. A "
         "diagnostic only (D13), never an exclusion by itself.", _locked),
    Rule("INFO-OUTSIDE-HOURS", 1, "validity", Action.INFO, _M,
         "1m bars outside 09:15-15:29 IST. The loader's session filter drops them, as today.",
         _cond(_OUTSIDE, in_session=False)),
    Rule("CAL-UNEXPECTED-DATE", 1, "calendar", Action.BLOCK, _BOTH,
         "Bars for most active symbols on a date the calendar does not list as a session "
         "(for example 2026-09-13): a calendar or dataset error that shifts every lookback.",
         _unexpected_date, needs_calendar=True),
    Rule("CAL-UNEXPECTED-ISOLATED", 1, "calendar", Action.EXCLUDE_SESSION, _BOTH,
         "A bar for one or a few symbols on a date the calendar does not list as a session.",
         _unexpected_isolated, needs_calendar=True),
    Rule("CMP-MISSING", 1, "completeness", Action.EXCLUDE_SYMBOL, _BOTH,
         "The symbol misses more than the policy share of the calendar sessions inside its "
         "own first-to-last range.", _missing_many, needs_calendar=True),
    Rule("CMP-MISSING-FEW", 1, "completeness", Action.WARNING, _BOTH,
         "The symbol misses some calendar sessions, within the policy share.", _missing_few,
         needs_calendar=True),
    Rule("CMP-LATEST", 1, "completeness", Action.EXCLUDE_SESSION, _BOTH,
         "The symbol's latest session was written before the close (daily) or has too few "
         "bars (1m): an unclosed bar that would leak an incomplete session into a backtest.",
         _latest_incomplete),
    Rule("FRESH-SYMBOL-LAG", 1, "freshness", Action.WARNING, _BOTH,
         "The symbol's data stops before the latest calendar session (a stopped feed or a "
         "delisting). A gap at the end is invisible to the first-to-last completeness check.",
         _symbol_lag),
    Rule("DIAG-ZERO-VOL", 1, "validity", Action.WARNING, _BOTH,
         "The symbol's share of zero-volume bars is above the policy share.", _zero_volume),
    Rule("DIAG-SPIKE", 1, "validity", Action.INFO, _D,
         "A daily move above the policy return that reverts by at least half on the next "
         "session: a possible bad tick (audit diagnostic only).", _spike),
    Rule("DIAG-STALE-REPEAT", 1, "validity", Action.INFO, _D,
         "Identical OHLC with zero volume on consecutive sessions: a possible stale or filled "
         "price (audit diagnostic only).", _stale),
    Rule("DIAG-LATE-START", 1, "identity_continuity", Action.INFO, _BOTH,
         "The symbol's first session is after the store start: a new listing or a rename "
         "(ETERNAL, LTM, TMPV, GMRAIRPORT). Phase 2 matches it against symbol_changes.yaml.",
         _late_start),
    Rule("DIAG-CROSS-INTERVAL", 1, "validity", Action.INFO, _D,
         "The daily volume disagrees with the summed in-session 1m volume beyond the policy "
         "band (audit diagnostic only).", _cross_interval),
    Rule("ADJ-JUMP", 1, "adjustment", Action.EXCLUDE_SYMBOL, _BOTH,
         "An overnight gap whose ratio matches a split or bonus factor (policy list): an "
         "unadjusted corporate action inside the series, so every lookback across it compares "
         "prices on two bases.", _jump),
    Rule("ADJ-CROSS-BASIS", 1, "adjustment", Action.EXCLUDE_SYMBOL, _M,
         "The daily close and the last in-session 1m close disagree beyond the policy share on "
         "at least the policy number of sessions: the two intervals are on different adjustment "
         "bases, so a strategy that combines them reads wrong price levels.", _cross_basis),
)

RULES: dict[str, Rule] = {r.rule_id: r for r in _CATALOGUE}

#: Rules whose findings exclude data because the data is wrong (they count toward D15).
DEFECT_RULES = frozenset(
    r.rule_id for r in _CATALOGUE
    if r.action in (Action.EXCLUDE_SESSION, Action.EXCLUDE_SYMBOL)
)

#: Warning-level missing-session findings that become defect exclusions when the contract forbids
#: dropping single sessions (owner decision for IDEA-027-style lookbacks: a missing daily session
#: shifts the lookback window, so the symbol is excluded rather than the gap silently skipped).
MISSING_SESSION_RULES = frozenset({"CMP-MISSING-FEW"})

#: The invalid-bar conditions the widespread-corruption check counts (each row counted once).
INVALID_BAR = f"({_OHLC_BAD} OR {_NONPOS} OR {_NEG_VOL} OR {_NULL_ROW})"


def evaluate(con, scope: Scope, rule_id: str, ctx: Context) -> RuleResult:
    """Run one rule. Raises KeyError for an unknown rule; DuckDB errors propagate."""
    rule = RULES[rule_id]
    reason = ""
    if scope.interval not in rule.intervals:
        reason = f"rule does not apply to interval {scope.interval}"
    elif rule.needs_calendar and not ctx.calendar:
        reason = "no calendar supplied"
    elif rule_id == "CMP-LATEST" and scope.interval == "D" \
            and "created_at" not in _columns(con, scope.table):
        reason = "the table has no created_at column"
    elif rule_id == "ADJ-CROSS-BASIS" and not _has_interval(con, scope, "D"):
        reason = "no daily bars to compare with"
    if reason:
        return RuleResult(rule.rule_id, rule.version, rule.dimension, rule.action,
                          NOT_EVALUATED, 0, _empty(), reason)
    evidence, rows = rule.run(con, scope, ctx)
    return RuleResult(rule.rule_id, rule.version, rule.dimension, rule.action, EVALUATED,
                      rows, evidence)


def invalid_bar_counts(con, scope: Scope) -> tuple[int, int]:
    """(invalid rows, total rows) in the scope, for the widespread-corruption check. 1m bars
    outside the continuous session are left out, as the loader drops them."""
    bars, params = _bars(scope, in_session=True)
    bad, total = con.execute(
        f"WITH {bars} SELECT SUM(CASE WHEN {INVALID_BAR} THEN 1 ELSE 0 END), COUNT(*) "
        "FROM bars", params).fetchone()
    return int(bad or 0), int(total or 0)
