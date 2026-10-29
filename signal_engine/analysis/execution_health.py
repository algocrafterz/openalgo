"""Daily execution-health check for one IST trading day. READ-ONLY.

Answers two questions:
  1. Did the rate-limit / API-flakiness / stale-data problems found on 2026-10-29 recur?
  2. How did they touch that day's paper trades?

Inputs (never written, never called over the network):
  log/openalgo_<day>.log[.*]               OpenAlgo server log (limiter, Flattrade, websocket)
  log/errors.jsonl                         OpenAlgo structured errors (cross-check only)
  signal_engine/logs/signal_engine_*_<day>.log  signal_engine log (any mode)
  signal_engine/logs/errors_<day>.jsonl    signal_engine structured errors (cross-check only)
  signal_engine/data/trades.db             opened with mode=ro
  signal_engine/data/tradebook/tradebook_<day>.json  broker fills snapshot, when saved

All log timestamps are naive local time on this host, which is IST. Structured error files may
carry an explicit offset; those are converted to IST on read.

Run:  uv run python -m signal_engine.analysis.execution_health --day 2026-10-29 --format md
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from signal_engine.timeutils import IST

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# THRESHOLDS - the single place that decides OK / WATCH / REGRESSION.
#
# A value strictly greater than `regress_gt` is REGRESSION; strictly greater than `watch_gt`
# is WATCH; otherwise OK. `baseline` is what 2026-10-29 showed before the fixes (pre-fix).
# The fixes were: FLATTRADE_MAX_PER_SECOND=7 plus data lanes, and the tracker stale-price guard.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Threshold:
    key: str
    label: str
    watch_gt: float | None
    regress_gt: float | None
    baseline: str
    why: str


THRESHOLDS: tuple[Threshold, ...] = (
    Threshold(
        "broker_rejections",
        "Flattrade per-second order rejections",
        0,
        5,
        "16-18",
        "Each one is an order refused for exceeding 10/s; all recovered on retry 1/3, so a few "
        "are tolerable, but more than 5 a day means the 7/s cap is not holding.",
    ),
    Threshold(
        "crit_max_wait",
        "Limiter critical-lane max wait (s)",
        2,
        5,
        "up to 18.9",
        "Critical calls are order price lookups; signal_engine times out at 5s, so a wait "
        "above 5s predicts order timeouts.",
    ),
    Threshold(
        "peak_calls_min",
        "Peak data calls per minute",
        90,
        110,
        "110-128",
        "The Flattrade data cap is 110/min; running at the cap means orders queue behind polling.",
    ),
    Threshold(
        "limiter_refused",
        "Limiter refused calls (total)",
        0,
        5,
        "0",
        "A refused call is dropped data or a failed lookup, not just a delay.",
    ),
    Threshold(
        "limiter_timeouts",
        "Limiter timeouts (total)",
        0,
        5,
        "0",
        "Calls that waited out their whole budget and failed.",
    ),
    Threshold(
        "slow_orders_5s",
        "Slow sandbox place_order above 5s",
        0,
        3,
        "16 slow (max 19s)",
        "Order placement above 5s collides with the 5s client timeout.",
    ),
    Threshold(
        "order_timeouts",
        "signal_engine 'Timeout sending order'",
        0,
        3,
        "11-17",
        "Each timeout leaves the order state ambiguous and needs reconciliation.",
    ),
    Threshold(
        "exit_uncertain",
        "EXIT UNCERTAIN (CRITICAL)",
        None,
        0,
        "4",
        "An exit that could not be confirmed and was not resent: a position may be unmanaged. "
        "Any occurrence is a regression.",
    ),
    Threshold(
        "positionbook_fail",
        "Positionbook fetch failed after 3 attempts",
        0,
        3,
        "17-18",
        "Position reconciliation is blind while this fails.",
    ),
    Threshold(
        "history_errors",
        "history_service errors",
        2,
        10,
        "41",
        "HTTP/2 shared-connection failures (ConnectionTerminated, EOF, deque mutated) starve "
        "the history cycles that drive entries.",
    ),
    Threshold(
        "ws_drops",
        "Flattrade websocket drops",
        3,
        10,
        "6 (log at the time; the full day is higher)",
        "Each drop is a gap in live ticks; the sandbox then double-polls over REST.",
    ),
    Threshold(
        "ws_max_outage_s",
        "Longest drop-to-data-flowing outage (s)",
        120,
        300,
        "minutes",
        "Time from a drop until the feed reported data flowing again.",
    ),
    Threshold(
        "dns_blips",
        "DNS 'Temporary failure in name resolution'",
        0,
        10,
        "10",
        "Host network flakiness, outside OpenAlgo.",
    ),
    Threshold(
        "stale_warnings",
        "Stale-price warnings (tracker)",
        0,
        10,
        "n/a (guard new)",
        "Held position price unchanged 120s: the feed is stale while a position is open.",
    ),
    Threshold(
        "late_cycles",
        "Pipeline cycles skipped as late (symbols)",
        0,
        10,
        "30 (10:50 and 10:55 cycles)",
        "A late history cycle skips entry evaluation, so signals are lost.",
    ),
    Threshold(
        "ack_p95_s",
        "Entry signal-to-ack p95 (s)",
        8,
        12,
        "median 4, worst 17",
        "Time from signal receipt to order acknowledgement in trades.db.",
    ),
    Threshold(
        "limiter_log_rate",
        "Limiter summary lines in the busiest minute",
        3,
        20,
        "2 (about 22k lines on 2026-10-28, 15:59-16:01)",
        "Summaries should appear about once a minute; a flood means the logger is looping.",
    ),
)

EXPECTED_CAP_PER_SECOND = 7  # .env FLATTRADE_MAX_PER_SECOND after the 2026-10-29 fix
STALE_PRICE_S = 120  # tracker marks a held price stale after this many seconds unchanged
SLOW_ORDER_MS = 5000  # a place_order above this is "slow" for incident flagging
PRE_MARGIN_S = 90  # an order incident this long before a leg's ack still belongs to it
POST_MARGIN_S = 30
EXIT_UNCERTAIN_TAIL_S = 300  # EXIT UNCERTAIN is attributed up to this long after the last exit
SESSION_END = (15, 30)

STATUS_ORDER = {"N/A": 0, "OK": 1, "WATCH": 2, "REGRESSION": 3}

# ---------------------------------------------------------------------------
# Line formats (taken from real 2026-10-29 logs).
# ---------------------------------------------------------------------------

_OA_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)?\] (\w+) in ([\w.]+): (.*)$")
_SE_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| (\w+)\s*\| (\S+)\s*\| (\S+)\s*\| (.*)$"
)
_REJECT = re.compile(
    r"Flattrade rate limit hit on (\S+?)@\S+ \(.*?Order Rec\w+ (\d+) in a current second "
    r"exceeds Limit (\d+).*?\). Retrying in [\d.]+s \(attempt (\d+)/(\d+)\)"
)
_LIMITER = re.compile(r"Flattrade data limiter, last (\d+)s: (.*)$")
_LANE = re.compile(
    r"(\w+) n=(\d+) avg_wait=([\d.]+)s max_wait=([\d.]+)s delayed=(\d+) refused=(\d+)"
)
_LIM_TAIL = re.compile(r"broker_rejections=(\d+) timeouts=(\d+)")
_CAP = re.compile(r"cap=(\S+)")
_SLOW = re.compile(r"Slow sandbox place_order (\S+) (\S+): total=(\d+)ms(?:.*?price=(\d+)ms)?")
_DROP = "WebSocket error: Connection to remote host was lost"
_FLOW = "Market data is flowing"
_DNS = "Temporary failure in name resolution"
_HIST = "Error fetching historical data"
_TIMEOUT = re.compile(r"Timeout sending order for (\S+)")
_UNCERTAIN = re.compile(r"EXIT UNCERTAIN: (BUY|SELL) (\d+) (\S+)")
_POSBOOK = "Failed to fetch positionbook after"
_STALE = re.compile(r"^(\S+): price [\d.]+ unchanged for (\d+)s while held")
_MOVING = re.compile(r"^(\S+): price is moving again")
_CYCLE = re.compile(
    r"pipeline cycle (\d\d:\d\d): evaluated (\d+), fires (\d+), submitted (\d+), "
    r"skipped (\d+), late (\d+) in ([\d.]+)s"
)


@dataclass(frozen=True)
class Event:
    ts: datetime
    kind: str
    symbol: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _ts(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def _iter_lines(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from fh
    except OSError:
        logger.warning("Cannot read {}", path)


def _slow_reason(msg: str) -> str:
    for needle, name in (
        ("ConnectionTerminated", "ConnectionTerminated"),
        ("EOF occurred", "EOF"),
        ("deque mutated", "deque mutated"),
        ("Server disconnected", "Server disconnected"),
    ):
        if needle in msg:
            return name
    return "other"


def parse_openalgo_line(line: str) -> Event | None:
    m = _OA_LINE.match(line.rstrip("\n"))
    if not m:
        return None
    stamp, _level, name, msg = m.groups()
    ts = _ts(stamp)
    if name == "rate_limit":
        r = _REJECT.search(msg)
        if r:
            return Event(
                ts,
                "rejection",
                r.group(1),
                {
                    "n": int(r.group(2)),
                    "limit": int(r.group(3)),
                    "attempt": int(r.group(4)),
                    "of": int(r.group(5)),
                },
            )
        lim = _LIMITER.search(msg)
        if lim:
            body = lim.group(2)
            lanes = {
                ln.group(1): {
                    "n": int(ln.group(2)),
                    "avg_wait": float(ln.group(3)),
                    "max_wait": float(ln.group(4)),
                    "delayed": int(ln.group(5)),
                    "refused": int(ln.group(6)),
                }
                for ln in _LANE.finditer(body)
            }
            tail = _LIM_TAIL.search(body)
            cap = _CAP.search(body)
            return Event(
                ts,
                "limiter",
                data={
                    "secs": int(lim.group(1)),
                    "lanes": lanes,
                    "broker_rejections": int(tail.group(1)) if tail else 0,
                    "timeouts": int(tail.group(2)) if tail else 0,
                    "cap": cap.group(1) if cap else "",
                    "key": body,
                },
            )
        return None
    if name == "order_manager":
        s = _SLOW.search(msg)
        if s:
            return Event(
                ts,
                "slow_order",
                s.group(1),
                {"total_ms": int(s.group(3)), "price_ms": int(s.group(4) or 0)},
            )
        return None
    if name == "flattrade_websocket":
        if _DROP in msg:
            return Event(ts, "ws_drop")
        if _FLOW in msg:
            return Event(ts, "ws_flow")
        return None
    if name == "history_service" and _HIST in msg:
        return Event(ts, "history_error", data={"reason": _slow_reason(msg)})
    if _DNS in msg:
        return Event(ts, "dns")
    return None


def parse_signal_line(line: str) -> Event | None:
    m = _SE_LINE.match(line.rstrip("\n"))
    if not m:
        return None
    stamp, level, sym, name, msg = m.groups()
    ts = _ts(stamp)
    symbol = "" if sym == "-" else sym
    t = _TIMEOUT.search(msg)
    if t and name == "executor":
        return Event(ts, "order_timeout", t.group(1))
    u = _UNCERTAIN.search(msg)
    if u and level == "CRITICAL":
        return Event(ts, "exit_uncertain", u.group(3), {"side": u.group(1)})
    if _POSBOOK in msg:
        return Event(ts, "positionbook_fail")
    s = _STALE.match(msg)
    if s:
        return Event(ts, "stale", s.group(1), {"secs": int(s.group(2))})
    mv = _MOVING.match(msg)
    if mv:
        return Event(ts, "stale_recovered", mv.group(1))
    c = _CYCLE.search(msg)
    if c:
        return Event(
            ts,
            "cycle",
            data={
                "slot": c.group(1),
                "evaluated": int(c.group(2)),
                "skipped": int(c.group(5)),
                "late": int(c.group(6)),
                "secs": float(c.group(7)),
            },
        )
    if _DNS in msg:
        return Event(ts, "dns", symbol)
    return None


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def oa_dir(self) -> Path:
        return self.root / "log"

    @property
    def se_dir(self) -> Path:
        return self.root / "signal_engine" / "logs"

    @property
    def trades_db(self) -> Path:
        return self.root / "signal_engine" / "data" / "trades.db"

    @property
    def tradebook_dir(self) -> Path:
        return self.root / "signal_engine" / "data" / "tradebook"

    def openalgo_logs(self, day: date) -> list[Path]:
        return sorted(self.oa_dir.glob(f"openalgo_{day.isoformat()}.log*"))

    def signal_logs(self, day: date) -> list[Path]:
        return sorted(self.se_dir.glob(f"signal_engine_*_{day.isoformat()}.log"))

    def error_files(self, day: date) -> list[Path]:
        files = [self.oa_dir / "errors.jsonl", self.se_dir / f"errors_{day.isoformat()}.jsonl"]
        return [f for f in files if f.exists()]


def load_events(paths: Paths, day: date) -> tuple[list[Event], dict[str, list[str]]]:
    """Parse every relevant log for `day`. Returns (events sorted by time, sources used)."""
    events: list[Event] = []
    used: dict[str, list[str]] = {"openalgo": [], "signal_engine": []}
    seen_limiter: set[tuple[datetime, str]] = set()
    for f in paths.openalgo_logs(day):
        used["openalgo"].append(f.name)
        for line in _iter_lines(f):
            ev = parse_openalgo_line(line)
            if ev is None or ev.ts.date() != day:
                continue
            if ev.kind == "limiter":
                ident = (ev.ts, ev.data["key"])
                if ident in seen_limiter:  # the same summary is written twice by two handlers
                    continue
                seen_limiter.add(ident)
            events.append(ev)
    for f in paths.signal_logs(day):
        used["signal_engine"].append(f.name)
        for line in _iter_lines(f):
            ev = parse_signal_line(line)
            if ev is not None and ev.ts.date() == day:
                events.append(ev)
    events.sort(key=lambda e: e.ts)
    return events, used


def _norm_error_ts(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


def error_file_counts(paths: Paths, day: date) -> dict[str, Any]:
    """Cross-check counts from the structured error files, normalised to IST."""
    total = 0
    drops = 0
    by_logger: Counter[str] = Counter()
    for f in paths.error_files(day):
        for line in _iter_lines(f):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ts = _norm_error_ts(str(row.get("ts") or row.get("time") or ""))
            if ts is None or ts.date() != day:
                continue
            total += 1
            by_logger[str(row.get("logger", "?"))] += 1
            if _DROP in str(row.get("message", "")) and row.get("logger") == "flattrade_websocket":
                drops += 1
    return {"total": total, "ws_drops": drops, "top_loggers": by_logger.most_common(5)}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[idx]


def _first_last(evs: list[Event]) -> dict[str, str | None]:
    if not evs:
        return {"first": None, "last": None}
    return {"first": evs[0].ts.strftime("%H:%M:%S"), "last": evs[-1].ts.strftime("%H:%M:%S")}


def _per_symbol(evs: list[Event]) -> list[tuple[str, int]]:
    return Counter(e.symbol for e in evs).most_common(8)


def compute_metrics(events: list[Event]) -> dict[str, Any]:
    """Aggregate parsed events into the metrics the verdict and report use."""
    by: dict[str, list[Event]] = {}
    for e in events:
        by.setdefault(e.kind, []).append(e)

    rej = by.get("rejection", [])
    lim = by.get("limiter", [])
    slow = by.get("slow_order", [])
    drops = by.get("ws_drop", [])
    flows = by.get("ws_flow", [])
    hist = by.get("history_error", [])
    cycles = by.get("cycle", [])

    # Limiter summaries: peak calls/min only from windows long enough to mean something.
    peak = 0.0
    crit_max = 0.0
    refused = tout = brej = 0
    caps: list[tuple[str, str]] = []
    minute_counts: Counter[str] = Counter()
    lane_peaks: dict[str, float] = {}
    for e in lim:
        d = e.data
        minute_counts[e.ts.strftime("%H:%M")] += 1
        lanes = d["lanes"]
        total_n = sum(v["n"] for v in lanes.values())
        if d["secs"] >= 30:
            peak = max(peak, total_n * 60.0 / d["secs"])
        if "critical" in lanes:
            crit_max = max(crit_max, lanes["critical"]["max_wait"])
        for name, v in lanes.items():
            lane_peaks[name] = max(lane_peaks.get(name, 0.0), v["max_wait"])
        refused += sum(v["refused"] for v in lanes.values())
        tout += d["timeouts"]
        brej += d["broker_rejections"]
        if d["cap"] and (not caps or caps[-1][1] != d["cap"]):
            caps.append((e.ts.strftime("%H:%M:%S"), d["cap"]))

    # Websocket: outage = drop to the next 'Market data is flowing'.
    outages: list[tuple[str, float]] = []
    for d in drops:
        nxt = next((f for f in flows if f.ts > d.ts), None)
        close = d.ts.replace(hour=SESSION_END[0], minute=SESSION_END[1], second=0)
        # After the close the feed legitimately stays down until the next morning.
        if nxt is not None and d.ts <= close and nxt.ts <= close:
            following = next((x for x in drops if x.ts > d.ts), None)
            if following is None or nxt.ts <= following.ts:
                outages.append((d.ts.strftime("%H:%M:%S"), (nxt.ts - d.ts).total_seconds()))
    # Collapse the 3-line echo of one drop (websocket + two adapter lines) is already done by
    # counting only the flattrade_websocket logger line.

    slow5 = [e for e in slow if e.data["total_ms"] > SLOW_ORDER_MS]
    stale = by.get("stale", [])
    rec = by.get("stale_recovered", [])
    late_syms = sum(e.data["late"] for e in cycles)
    skipped_syms = sum(e.data["skipped"] for e in cycles)

    return {
        "broker_rejections": len(rej),
        "rejection_detail": {
            **_first_last(rej),
            "max_in_second": max((e.data["n"] for e in rej), default=None),
            "max_attempt": max((e.data["attempt"] for e in rej), default=None),
            "exhausted": sum(1 for e in rej if e.data["attempt"] >= e.data["of"]),
            "top_symbols": _per_symbol(rej),
        },
        "limiter_summaries": len(lim),
        "crit_max_wait": crit_max if lim else None,
        "lane_max_wait": lane_peaks,
        "peak_calls_min": round(peak, 1) if lim else None,
        "limiter_refused": refused if lim else None,
        "limiter_timeouts": tout if lim else None,
        "limiter_broker_rejections": brej if lim else None,
        "caps": caps,
        "last_cap": caps[-1][1] if caps else None,
        "limiter_log_rate": max(minute_counts.values()) if minute_counts else None,
        "slow_orders": len(slow),
        "slow_orders_5s": len(slow5),
        "slow_order_max_ms": max((e.data["total_ms"] for e in slow), default=None),
        "slow_order_max_price_ms": max((e.data["price_ms"] for e in slow), default=None),
        "order_timeouts": len(by.get("order_timeout", [])),
        "order_timeout_detail": {
            **_first_last(by.get("order_timeout", [])),
            "top_symbols": _per_symbol(by.get("order_timeout", [])),
        },
        "exit_uncertain": len(by.get("exit_uncertain", [])),
        "exit_uncertain_detail": {
            **_first_last(by.get("exit_uncertain", [])),
            "symbols": sorted({e.symbol for e in by.get("exit_uncertain", [])}),
        },
        "positionbook_fail": len(by.get("positionbook_fail", [])),
        "positionbook_detail": _first_last(by.get("positionbook_fail", [])),
        "history_errors": len(hist),
        "history_reasons": Counter(e.data["reason"] for e in hist).most_common(),
        "history_detail": _first_last(hist),
        "ws_drops": len(drops),
        "ws_detail": _first_last(drops),
        "ws_outages": sorted(outages, key=lambda x: -x[1])[:5],
        "ws_max_outage_s": max((o[1] for o in outages), default=(0.0 if not drops else None)),
        "dns_blips": len(by.get("dns", [])),
        "dns_detail": _first_last(by.get("dns", [])),
        "stale_warnings": len(stale),
        "stale_recovered": len(rec),
        "stale_by_symbol": _per_symbol(stale),
        "stale_detail": _first_last(stale),
        "cycles": len(cycles),
        "late_cycles": late_syms if cycles else None,
        "skipped_symbols": skipped_syms,
        "late_cycle_slots": [e.data["slot"] for e in cycles if e.data["late"] > 0],
        "cycle_max_secs": max((e.data["secs"] for e in cycles), default=None),
    }


def evaluate(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn metrics into per-threshold rows with a status."""
    rows = []
    for t in THRESHOLDS:
        v = metrics.get(t.key)
        if v is None:
            status = "N/A"
        elif t.regress_gt is not None and v > t.regress_gt:
            status = "REGRESSION"
        elif t.watch_gt is not None and v > t.watch_gt:
            status = "WATCH"
        else:
            status = "OK"
        rows.append(
            {
                "key": t.key,
                "label": t.label,
                "value": v,
                "status": status,
                "watch_gt": t.watch_gt,
                "regress_gt": t.regress_gt,
                "baseline": t.baseline,
                "why": t.why,
            }
        )
    return rows


def cap_check(metrics: dict[str, Any]) -> dict[str, str]:
    """Is the 2026-10-29 cap change (7/s) in effect at the end of the day?"""
    cap = metrics.get("last_cap")
    if cap is None:
        return {"status": "N/A", "detail": "no limiter summary lines"}
    per_sec = cap.split("/s")[0]
    if per_sec == str(EXPECTED_CAP_PER_SECOND):
        return {"status": "OK", "detail": f"last cap {cap}"}
    return {
        "status": "WATCH",
        "detail": f"last cap {cap}, expected {EXPECTED_CAP_PER_SECOND}/s "
        "(FLATTRADE_MAX_PER_SECOND change not active)",
    }


def overall(rows: list[dict[str, Any]], cap: dict[str, str]) -> str:
    worst = "OK"
    for s in [r["status"] for r in rows] + [cap["status"]]:
        if STATUS_ORDER[s] > STATUS_ORDER[worst]:
            worst = s
    return worst


# ---------------------------------------------------------------------------
# trades.db (read-only) and paper-trade impact
# ---------------------------------------------------------------------------


def load_trade_rows(db: Path, day: date) -> list[dict[str, Any]] | None:
    """Rows for `day` from trades.db via a read-only connection; None if unreadable."""
    if not db.exists() or db.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        logger.warning("Cannot open {} read-only", db)
        return None
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE COALESCE(executed_at, received_at) LIKE ? "
                "ORDER BY COALESCE(executed_at, received_at)",
                (f"{day.isoformat()}%",),
            )
        ]
    except sqlite3.Error:
        logger.warning("trades table unreadable in {}", db)
        return None
    finally:
        conn.close()
    return rows


def _parse_naive(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


def ack_latencies(rows: list[dict[str, Any]]) -> list[tuple[float, str, str]]:
    """(seconds, symbol, direction) from received_at to executed_at for acknowledged entries."""
    out = []
    for r in rows:
        # Entries only: exit rows record receipt and ack at the same instant, which would
        # drag the median to zero.
        if str(r.get("status")) != "SUCCESS" or r.get("direction") not in ("LONG", "SHORT"):
            continue
        a, b = _parse_naive(r.get("received_at")), _parse_naive(r.get("executed_at"))
        if a is None or b is None:
            continue
        secs = (b - a).total_seconds()
        if 0 <= secs <= 600:
            out.append((secs, str(r.get("symbol")), str(r.get("direction") or "")))
    return out


def latency_summary(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    if rows is None:
        return {"derivable": False}
    lat = ack_latencies(rows)
    if not lat:
        return {"derivable": False}
    secs = [x[0] for x in lat]
    return {
        "derivable": True,
        "n": len(secs),
        "median_s": round(statistics.median(secs), 2),
        "p95_s": round(_pct(secs, 0.95) or 0.0, 2),
        "worst5": [(round(s, 1), sym, d) for s, sym, d in sorted(lat, reverse=True)[:5]],
        "timeout_rows": sum(1 for r in rows if str(r.get("status")) == "TIMEOUT"),
    }


def _window_hit(ev: Event, lo: datetime, hi: datetime) -> bool:
    return lo <= ev.ts <= hi


def _near(ev: Event, sym: str, ats: list[datetime]) -> bool:
    """True if `ev` is for `sym` and falls in the margin window around any of `ats`."""
    return ev.symbol == sym and any(
        _window_hit(ev, at - timedelta(seconds=PRE_MARGIN_S), at + timedelta(seconds=POST_MARGIN_S))
        for at in ats
    )


def flag_positions(positions: list[Any], events: list[Event], day: date) -> list[dict[str, Any]]:
    """Attach incident flags to each ledger position. Pure function of its inputs."""
    session_end = datetime(day.year, day.month, day.day, *SESSION_END, tzinfo=IST)
    by_kind: dict[str, list[Event]] = {}
    for e in events:
        by_kind.setdefault(e.kind, []).append(e)

    out = []
    for p in positions:
        entry_at = p.entry.at
        row: dict[str, Any] = {"position": p, "flags": [], "assessable": entry_at is not None}
        if entry_at is None:
            out.append(row)
            continue
        exit_ats = [x.at for x in p.exits if x.at is not None]
        end = max(exit_ats) if exit_ats else session_end
        sym = p.symbol
        legs = [entry_at, *exit_ats]

        flags = row["flags"]
        to = by_kind.get("order_timeout", [])
        if any(_near(e, sym, [entry_at]) for e in to):
            flags.append("entry_timeout")
        if any(_near(e, sym, exit_ats) for e in to):
            flags.append("exit_timeout")
        tail = timedelta(seconds=EXIT_UNCERTAIN_TAIL_S)
        if any(
            e.symbol == sym and _window_hit(e, entry_at, end + tail)
            for e in by_kind.get("exit_uncertain", [])
        ):
            flags.append("exit_uncertain")
        if any(_window_hit(e, entry_at, end) for e in by_kind.get("positionbook_fail", [])):
            flags.append("positionbook_fail_while_held")
        if any(e.symbol == sym and _window_hit(e, entry_at, end) for e in by_kind.get("stale", [])):
            flags.append("stale_price_while_held")
        if any(
            _near(e, sym, legs)
            for e in by_kind.get("slow_order", [])
            if e.data["total_ms"] > SLOW_ORDER_MS
        ):
            flags.append("slow_place_order")
        out.append(row)
    return out


def _stats(items: list[Any]) -> dict[str, Any]:
    pnl = [i.gross_pnl for i in items if i.gross_pnl is not None]
    rs = [i.realised_r for i in items if i.realised_r is not None]
    ent = [i.entry_slippage_bps for i in items if i.entry_slippage_bps is not None]
    return {
        "n_trades": len(items),
        "n_with_pnl": len(pnl),
        "wins": sum(1 for x in pnl if x > 0),
        "total_pnl": round(sum(pnl), 2) if pnl else None,
        "mean_pnl": round(statistics.fmean(pnl), 2) if pnl else None,
        "n_with_r": len(rs),
        "mean_r": round(statistics.fmean(rs), 3) if rs else None,
        "n_entry_slip": len(ent),
        "mean_entry_slip_bps": round(statistics.fmean(ent), 1) if ent else None,
    }


def trade_impact(paths: Paths, day: date, events: list[Event]) -> dict[str, Any]:
    """Which of the day's paper trades an incident touched, and what it cost."""
    rows = load_trade_rows(paths.trades_db, day)
    if rows is None:
        return {"available": False, "reason": "trades.db missing, empty or unreadable"}
    try:
        from signal_engine.analysis import ledger
    except ImportError:  # pragma: no cover - defensive
        return {"available": False, "reason": "ledger module not importable"}

    evs = []
    for r in rows:
        if str(r.get("status") or "").upper() == "DECLINED":
            continue
        ts = _parse_naive(r.get("executed_at") or r.get("received_at"))
        if ts is None:
            continue
        try:
            r["context"] = json.loads(r.get("context") or "{}")
        except (ValueError, TypeError):
            r["context"] = {}
        r["_ts"] = ts
        evs.append(r)

    fills: dict[str, dict] = {}
    tb = paths.tradebook_dir / f"tradebook_{day.isoformat()}.json"
    fills_source = "none (using fill_price saved in trades.db)"
    if tb.exists():
        try:
            fills = ledger.load_fills(json.loads(tb.read_text(encoding="utf-8")))
            fills_source = tb.name
        except (ValueError, OSError):
            logger.warning("Tradebook snapshot {} unreadable", tb)

    positions = [
        p for p in ledger.build_ledger(evs, fills) if p.day == day and p.entry.at is not None
    ]
    flagged_rows = flag_positions(positions, events, day)
    touched = [r["position"] for r in flagged_rows if r["flags"]]
    clean = [r["position"] for r in flagged_rows if r["assessable"] and not r["flags"]]

    details = []
    for r in flagged_rows:
        if not r["flags"]:
            continue
        p = r["position"]
        exit_slip = p.exit_slippage_bps
        details.append(
            {
                "strategy": p.strategy,
                "symbol": p.symbol,
                "direction": "LONG" if p.direction > 0 else "SHORT",
                "flags": r["flags"],
                "entry_intended": p.entry.signal_price or None,
                "entry_fill": p.entry.fill_price,
                "entry_slip_bps": None
                if p.entry_slippage_bps is None
                else round(p.entry_slippage_bps, 1),
                "exit_reasons": p.exit_reasons,
                "exit_slip_bps": None if exit_slip is None else round(exit_slip, 1),
                "pnl": None if p.gross_pnl is None else round(p.gross_pnl, 2),
                "r": None if p.realised_r is None else round(p.realised_r, 2),
            }
        )
    total = len(touched) + len(clean)
    flag_counts = Counter(f for r in flagged_rows for f in r["flags"])
    return {
        "available": True,
        "fills_source": fills_source,
        "n_total": total,
        "n_flagged": len(touched),
        "share_flagged": round(len(touched) / total, 3) if total else None,
        "flag_counts": dict(flag_counts),
        "flagged": _stats(touched),
        "untouched": _stats(clean),
        "details": details,
        "latency": latency_summary(rows),
        "declined": sum(1 for r in rows if str(r.get("status")) == "DECLINED"),
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def previous_trading_day(paths: Paths, day: date, lookback: int = 10) -> date | None:
    for i in range(1, lookback + 1):
        d = day - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if paths.openalgo_logs(d) or paths.signal_logs(d):
            return d
    return None


def build_report(paths: Paths, day: date) -> dict[str, Any]:
    events, used = load_events(paths, day)
    rows_exist = load_trade_rows(paths.trades_db, day)
    if not events and not rows_exist:
        return {
            "day": day.isoformat(),
            "empty": True,
            "message": f"No execution data for {day.isoformat()}: no logs or trades found "
            "(weekend, holiday, or the platform did not run).",
        }

    metrics = compute_metrics(events)
    impact = trade_impact(paths, day, events)
    if impact.get("available") and impact["latency"].get("derivable"):
        metrics["ack_p95_s"] = impact["latency"]["p95_s"]
    else:
        metrics["ack_p95_s"] = None
    errs = error_file_counts(paths, day)
    rows = evaluate(metrics)
    cap = cap_check(metrics)

    prev_day = previous_trading_day(paths, day)
    prev_rows = None
    if prev_day is not None:
        pev, _ = load_events(paths, prev_day)
        pm = compute_metrics(pev)
        pimpact = load_trade_rows(paths.trades_db, prev_day)
        pm["ack_p95_s"] = latency_summary(pimpact).get("p95_s")
        prev_rows = {r["key"]: r["value"] for r in evaluate(pm)}
    return {
        "day": day.isoformat(),
        "empty": False,
        "sources": used,
        "overall": overall(rows, cap),
        "verdict": rows,
        "cap_check": cap,
        "metrics": metrics,
        "errors_jsonl": errs,
        "impact": impact,
        "previous_day": prev_day.isoformat() if prev_day else None,
        "previous_values": prev_rows,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _thr(row: dict[str, Any]) -> str:
    w = "-" if row["watch_gt"] is None else f">{row['watch_gt']:g}"
    r = "-" if row["regress_gt"] is None else f">{row['regress_gt']:g}"
    return f"{w} / {r}"


def render_md(rep: dict[str, Any]) -> str:
    if rep["empty"]:
        return f"# Execution health {rep['day']}\n\n{rep['message']}\n"
    m, imp = rep["metrics"], rep["impact"]
    prev = rep["previous_values"] or {}
    L: list[str] = []
    L.append(f"# Execution health {rep['day']}")
    L.append("")
    L.append(
        f"Overall: **{rep['overall']}**  (cap check: {rep['cap_check']['status']} - "
        f"{rep['cap_check']['detail']})"
    )
    L.append("")
    L.append(
        f"Previous trading day with logs: {rep['previous_day'] or 'none found'}. "
        "Baseline column is 2026-10-29 before the fixes. Thresholds are "
        "WATCH / REGRESSION (value strictly greater)."
    )
    L.append("")
    L.append("| Metric | Today | Status | Prev day | Baseline 10-29 | Watch / Regress |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in rep["verdict"]:
        L.append(
            f"| {r['label']} | {_fmt(r['value'])} | {r['status']} | "
            f"{_fmt(prev.get(r['key'])) if rep['previous_day'] else 'n/a'} | "
            f"{r['baseline']} | {_thr(r)} |"
        )
    L.append("")
    L.append(
        "Sources: openalgo "
        + (", ".join(rep["sources"]["openalgo"]) or "none")
        + "; signal_engine "
        + (", ".join(rep["sources"]["signal_engine"]) or "none")
        + "."
    )
    L.append("")
    L.append("## Details")
    rd = m["rejection_detail"]
    L.append(
        f"- Order rejections: {m['broker_rejections']} (first {rd['first']}, last "
        f"{rd['last']}, highest N in a second {_fmt(rd['max_in_second'])}, max attempt "
        f"{_fmt(rd['max_attempt'])}, exhausted retries {rd['exhausted']}); "
        f"top symbols {rd['top_symbols']}"
    )
    L.append(
        f"- Limiter: {m['limiter_summaries']} summaries, peak {_fmt(m['peak_calls_min'])} "
        f"calls/min, lane max waits {m['lane_max_wait']}, refused {_fmt(m['limiter_refused'])}, "
        f"timeouts {_fmt(m['limiter_timeouts'])}, broker_rejections "
        f"{_fmt(m['limiter_broker_rejections'])}"
    )
    L.append(f"- Effective cap over the day (time, cap): {m['caps']}")
    L.append(
        f"- Slow place_order: {m['slow_orders']} slow, {m['slow_orders_5s']} above "
        f"{SLOW_ORDER_MS // 1000}s, max total {_fmt(m['slow_order_max_ms'])} ms, max price "
        f"step {_fmt(m['slow_order_max_price_ms'])} ms"
    )
    od = m["order_timeout_detail"]
    L.append(
        f"- Order timeouts: {m['order_timeouts']} (first {od['first']}, last {od['last']}), "
        f"by symbol {od['top_symbols']}"
    )
    ed = m["exit_uncertain_detail"]
    L.append(
        f"- EXIT UNCERTAIN: {m['exit_uncertain']} (first {ed['first']}, last {ed['last']}), "
        f"symbols {ed['symbols']}"
    )
    pd_ = m["positionbook_detail"]
    L.append(
        f"- Positionbook failures: {m['positionbook_fail']} (first {pd_['first']}, last "
        f"{pd_['last']})"
    )
    hd = m["history_detail"]
    L.append(
        f"- history_service errors: {m['history_errors']} (first {hd['first']}, last "
        f"{hd['last']}), reasons {m['history_reasons']}"
    )
    wd = m["ws_detail"]
    L.append(
        f"- Websocket drops: {m['ws_drops']} (first {wd['first']}, last {wd['last']}); "
        f"longest drop-to-flowing outages (time, s) {m['ws_outages']}"
    )
    L.append(
        f"- DNS blips: {m['dns_blips']} (first {m['dns_detail']['first']}, last "
        f"{m['dns_detail']['last']})"
    )
    L.append(
        f"- Stale prices: {m['stale_warnings']} warnings, {m['stale_recovered']} recoveries; "
        f"by symbol {m['stale_by_symbol']}"
    )
    L.append(
        f"- Pipeline cycles: {m['cycles']}; late symbols {_fmt(m['late_cycles'])}, skipped "
        f"symbols {m['skipped_symbols']}, late slots {m['late_cycle_slots']}, slowest cycle "
        f"{_fmt(m['cycle_max_secs'])} s"
    )
    e = rep["errors_jsonl"]
    L.append(
        f"- errors.jsonl cross-check (normalised to IST): {e['total']} entries for the day, "
        f"{e['ws_drops']} websocket drops; note log/errors.jsonl is truncated to the last "
        "1000 entries at startup, so its count can be lower than the log's"
    )
    L.append("")
    L.append("## Paper-trade impact")
    if not imp.get("available"):
        L.append(f"Not available: {imp.get('reason')}.")
    else:
        lat = imp["latency"]
        if lat.get("derivable"):
            L.append(
                f"- Signal-to-ack (n={lat['n']}): median {lat['median_s']}s, p95 "
                f"{lat['p95_s']}s, worst 5 {lat['worst5']}; TIMEOUT rows {lat['timeout_rows']}"
            )
        else:
            L.append("- Signal-to-ack: not derivable (no acknowledged orders in trades.db)")
        L.append(f"- Fill source: {imp['fills_source']}; declined signals: {imp['declined']}")
        sh = imp["share_flagged"]
        L.append(
            f"- Positions: {imp['n_total']}; touched by an incident {imp['n_flagged']} "
            f"({'n/a' if sh is None else f'{sh:.0%}'}); flag counts {imp['flag_counts']}"
        )
        L.append("")
        L.append(
            "| Group | n trades | n with P&L | wins | total P&L | mean P&L | n with R | "
            "mean R | n entry slip | mean entry slip bps |"
        )
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for name in ("flagged", "untouched"):
            s = imp[name]
            L.append(
                f"| {name} | {s['n_trades']} | {s['n_with_pnl']} | {s['wins']} | "
                f"{_fmt(s['total_pnl'])} | {_fmt(s['mean_pnl'])} | {s['n_with_r']} | "
                f"{_fmt(s['mean_r'])} | {s['n_entry_slip']} | "
                f"{_fmt(s['mean_entry_slip_bps'])} |"
            )
        L.append("")
        L.append(
            "Small samples: these are counts of a handful of positions. Differences are "
            "descriptive only and do not show that an incident caused a gain or loss. "
            "P&L is gross (before costs). Positive slippage bps means the fill cost money. "
            "Exit slippage is n/a where the exit has no trigger price or is a stop."
        )
        if imp["details"]:
            L.append("")
            L.append(
                "| Strategy | Symbol | Side | Flags | Entry intended | Entry fill | "
                "Entry slip bps | Exit | Exit slip bps | P&L | R |"
            )
            L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for d in imp["details"][:60]:
                L.append(
                    f"| {d['strategy']} | {d['symbol']} | {d['direction']} | "
                    f"{','.join(d['flags'])} | {_fmt(d['entry_intended'])} | "
                    f"{_fmt(d['entry_fill'])} | {_fmt(d['entry_slip_bps'])} | "
                    f"{d['exit_reasons']} | {_fmt(d['exit_slip_bps'])} | {_fmt(d['pnl'])} | "
                    f"{_fmt(d['r'])} |"
                )
            if len(imp["details"]) > 60:
                L.append(f"... {len(imp['details']) - 60} more flagged trades not shown")
    L.append("")
    L.append("## Why each threshold")
    for r in rep["verdict"]:
        L.append(f"- {r['label']}: {r['why']}")
    L.append("")
    return "\n".join(L)


def _default_day() -> date:
    return datetime.now(IST).date()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily read-only execution-health check")
    ap.add_argument("--day", type=date.fromisoformat, default=None, help="IST day, YYYY-MM-DD")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (for tests)")
    args = ap.parse_args(argv)
    day = args.day or _default_day()
    rep = build_report(Paths(args.root), day)
    if args.format == "json":
        sys.stdout.write(json.dumps(rep, indent=2, default=_json_default) + "\n")
    else:
        sys.stdout.write(render_md(rep) + "\n")
    return 0


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime | date):
        return o.isoformat()
    return str(o)


if __name__ == "__main__":
    raise SystemExit(main())
