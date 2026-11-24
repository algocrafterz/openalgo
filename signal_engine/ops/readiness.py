"""Phases `open-day` (09:12) and `liveness` (09:27): is the stack ready, and is it actually trading?

open-day owns readiness: services, broker session, mode, the engine's own startup result, the
feed ports, clock, disk, databases, the morning Windows tasks and yesterday's close. It reads the
engine's 08:5x startup checks from its log instead of running a second smoke test.

liveness owns the first minutes after the open: the first runner cycle, the BreakingTrade poller's
first scan, signals reaching the pipeline, and timeout or retry storms against yesterday's window.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import urllib.request
from datetime import date, datetime, timedelta

from signal_engine.ops import automation, ipalert, jobs, logs, logscan, recovery, wakecheck
from signal_engine.ops.model import FAIL, IST, NOT_VERIFIED, OK, REPORTS, WARN, Check, write_phase
from utils.logging import get_logger

logger = get_logger(__name__)

_WS_DROP = "connection to remote host was lost"
_WS_DROP_LIMIT = 10

HEALTH_URL = "http://127.0.0.1:5000/health/status"
PORTS = {"websocket proxy": 8765, "ZeroMQ bus": 5555}
POLLER_LOG = os.path.join(logs.SE_LOGS, "breakingtrade_poller.log")
DATA = os.path.join(logs.REPO, "signal_engine", "data")
MIN_FREE_GB = 5

#: The check that the price history the engine warms up from is current (see _price_history).
HISTORY_CHECK = "price history for today"
#: A read of the store waits this long for another program to let go of the file, polling every
#: HISTORY_LOCK_POLL_SECONDS: the one bounded retry. The engine's own warm-up hit such a lock on
#: 2026-11-13 10:15 and succeeded 3 s later, so 30 s covers the lock that has been observed.
HISTORY_LOCK_WAIT_SECONDS = 30.0
HISTORY_LOCK_POLL_SECONDS = 5.0
#: A read that runs longer than this is cut off and tried once more.
HISTORY_QUERY_SECONDS = 60.0
#: Bars older than the previous session by more than this many days are reported as "no bars since".
HISTORY_LOOKBACK_DAYS = 10
#: How many stock names the evidence lists before it says "and N more".
HISTORY_SHOWN = 6
BACKFILL_BY_HAND = "./signal_engine/backtest/backfill_cron.sh"


def _previous_weekday(day: str) -> str:
    current = date.fromisoformat(day) - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.isoformat()


def _previous_close_day(day: str) -> str:
    """The previous trading day: the previous weekday that is not an NSE holiday.

    A holiday has no close-day run, so the line must read the last session, the same day the
    08:36 recovery works on (recovery.previous_trading_day).
    """
    current = _previous_weekday(day)
    for _ in range(14):  # no real run of holidays is longer; the bound stops a broken calendar
        if automation.is_trading_day(current):
            return current
        current = _previous_weekday(current)
    return current


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _services() -> Check:
    out = _run([os.path.join(logs.REPO, "signal_engine", "scripts", "openalgoctl.sh"), "status"])
    up = "STATUS: OpenAlgo running" in out and "signal_engine PID" in out
    line = next(
        (ln.split("STATUS:", 1)[1].strip() for ln in out.splitlines() if "STATUS:" in ln),
        "no status",
    )
    return Check("services (openalgoctl status)", OK if up else FAIL, line)


def _health() -> Check:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:  # noqa: S310 - fixed local URL
            body = json.loads(response.read().decode())
        status = body.get("status")
        detail = f"{HEALTH_URL} status={status}"
        if status == "pass":
            return Check("OpenAlgo health", OK, detail)
        if status == "warn":
            # HTTP 200 with a warning: answering, with a monitored metric above its warning level
            # (2026-11-04: thread count 70 against 50). Only "fail" (HTTP 503), an unknown status
            # or no answer at all is a failure.
            return Check("OpenAlgo health warning", WARN, detail)
        return Check("OpenAlgo health", FAIL, detail)
    except Exception as exc:  # noqa: BLE001 - any failure is the finding
        return Check("OpenAlgo health", FAIL, f"{HEALTH_URL}: {type(exc).__name__}")


def _broker_and_mode(day: str | None = None) -> list[Check]:
    from signal_engine.api_client import fetch_available_capital, fetch_trading_mode

    async def read():
        return await fetch_available_capital(), await fetch_trading_mode()

    try:
        capital, (mode, _is_analyze) = asyncio.run(read())
    except Exception as exc:  # noqa: BLE001
        text = f"{type(exc).__name__}: {exc}"
        ipalert.log_invalid_ip(text, day or datetime.now(IST).strftime("%Y-%m-%d"))
        return [Check("broker session (funds API)", FAIL, text[:160])]
    funds = Check(
        "broker session (funds API)",
        OK if capital and capital > 0 else FAIL,
        f"available capital {capital:,.0f}" if capital else "funds API returned nothing",
    )
    mode_check = Check(
        "trading mode",
        WARN if mode == "live" else OK,
        "LIVE: real orders today" if mode == "live" else f"{mode} (sandbox)",
    )
    return [funds, mode_check]


def _engine_startup(day: str) -> Check:
    lines = logs.engine_lines(day, "06:00", "09:12")
    if not lines:
        return Check("engine startup", FAIL, f"no engine log lines today ({logs.engine_log(day)})")
    if any("Startup checks FAILED" in ln for ln in lines):
        return Check("engine startup", FAIL, "log: Startup checks FAILED")
    warm = [ln for ln in lines if "runner warm-up finished" in ln]
    tracker = any("Position tracker started" in ln for ln in lines)
    if not warm or not tracker:
        return Check("engine startup", FAIL, "no runner warm-up or tracker start in the log")
    match = re.search(r"(\d+) ok, (\d+) failed", warm[-1])
    failed = int(match.group(2)) if match else 0
    warnings = sum(1 for ln in lines if "Startup warnings" in ln)
    status = WARN if failed or warnings else OK
    return Check(
        "engine startup",
        status,
        f"{warm[-1][:19]} {match.group(0) if match else ''}; warnings {warnings}",
    )


def _poller_started(day: str) -> Check:
    if not os.path.exists(POLLER_LOG):
        return Check("BreakingTrade poller", FAIL, "no poller log")
    with open(POLLER_LOG, encoding="utf-8", errors="replace") as handle:
        started = [ln for ln in handle if ln.startswith(day) and "poller starting" in ln]
    return Check(
        "BreakingTrade poller",
        OK if started else FAIL,
        started[-1][11:19] + " poller starting" if started else "not started today",
    )


def _ports() -> list[Check]:
    checks = []
    for name, port in PORTS.items():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                checks.append(Check(f"{name} :{port}", OK, "accepting connections"))
        except OSError as exc:
            checks.append(Check(f"{name} :{port}", FAIL, type(exc).__name__))
    return checks


def _clock() -> Check:
    synced = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip()
    if not synced:
        return Check("clock (NTP)", NOT_VERIFIED, "timedatectl unavailable")
    return Check("clock (NTP)", OK if synced == "yes" else FAIL, f"NTPSynchronized={synced}")


def _disk_and_databases(day: str) -> list[Check]:
    free_gb = shutil.disk_usage(logs.REPO).free / 1e9
    checks = [Check("disk", OK if free_gb >= MIN_FREE_GB else FAIL, f"{free_gb:.1f} GB free")]
    try:
        with sqlite3.connect(f"file:{os.path.join(DATA, 'trades.db')}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT count(*) FROM trades").fetchone()[0]
        checks.append(Check("trades.db", OK, f"readable, {rows} rows"))
    except sqlite3.Error as exc:
        checks.append(Check("trades.db", FAIL, str(exc)))
    try:
        with sqlite3.connect(f"file:{os.path.join(DATA, 'risk.db')}?mode=ro", uri=True) as conn:
            used = conn.execute(
                "SELECT strategy, trades_today, daily_loss FROM risk_counters WHERE trade_date=? "
                "AND (trades_today > 0 OR daily_loss > 0)",
                (day,),
            ).fetchall()
        checks.append(
            Check(
                "risk counters",
                WARN if used else OK,
                f"already used before the open: {used}" if used else "fresh for today",
            )
        )
    except sqlite3.Error as exc:
        checks.append(Check("risk counters", FAIL, str(exc)))
    return checks


def _overnight_errors(day: str) -> Check:
    prev = _previous_weekday(day)
    records = [r for r in logs.app_errors(prev) if r[0] >= "15:30:00"] + list(logs.app_errors(day))
    records += list(logs.engine_errors(day))
    if not records:
        return Check("errors since the last close", OK, "none")
    top = {}
    benign = drops = 0
    for _when, name, message, symbol in records:
        if logscan.BENIGN.search(f"{name}: {message}"):
            benign += 1
            if _WS_DROP in (message or "").lower():
                drops += 1
            continue
        sig = logs.signature(name, message, symbol)
        top[sig] = top.get(sig, 0) + 1
    other = sum(top.values())
    worst = sorted(top.items(), key=lambda item: -item[1])[:3]
    head = f"{benign} known-benign, {other} other"
    # log/errors.jsonl holds ERROR+ records only, and the adapter's "Reconnected successfully"
    # line is INFO, so a reconnect cannot be verified here. It is reported UNKNOWN, never
    # assumed; a burst of drops (> _WS_DROP_LIMIT) is the only signal that raises a WARN.
    note = f"{drops} websocket drops, reconnect: UNKNOWN (not in error log)" if drops else ""
    if other:
        text = f"{head}; top: " + "; ".join(f"x{n} {s[:70]}" for s, n in worst)
        return Check("errors since the last close", WARN, f"{text}; {note}" if note else text)
    if drops > _WS_DROP_LIMIT:
        return Check(
            "errors since the last close",
            WARN,
            f"{head}; {drops} websocket drops (more than {_WS_DROP_LIMIT}), "
            "reconnect: UNKNOWN (not in error log)",
        )
    return Check("errors since the last close", OK, f"{head}; {note}" if note else head)


def _morning_tasks(day: str) -> list[Check]:
    out = _run([jobs.schtasks_exe(), "/query", "/fo", "csv", "/v"], timeout=60)
    if not out:
        return [Check("Windows tasks", NOT_VERIFIED, "schtasks.exe unavailable")]
    rows = jobs.parse_schtasks(out)
    return [
        jobs.task_check(name, jobs.WINDOWS_TASKS[name], rows.get(name), day)
        for name in ("openAlgoAutoStart", "breakingTradeAutoStart")
    ]


def _yesterday_close(day: str) -> Check:
    """The previous trading day's close-day result: always a WARN or OK, never a FAIL.

    The maintainer chose WARN: a failed history job of yesterday (data download, reports, review)
    must not turn the whole morning RED. That is not the same as harmless. The download and the
    value-area step write the historical store the engine warms up from, and a stale store once
    made PFC trade on old levels (header of backtest/backfill_cron.sh), so the text says so. The
    checks of today (broker session, price feed, OpenAlgo health, engine start, databases) stay
    separate and keep their own FAIL. Nothing in open-day compares the store with the previous
    trading day, so this WARN is the only morning sign of a stale store (open decision for the
    maintainer: see the Phase 1b report).

    The check runs at 09:12, after the 08:36 recovery had its turn, so the text does not promise
    a recovery: it points to the Daily Workflow page, which shows what the recovery did.
    """
    prev = _previous_close_day(day)
    path = os.path.join(REPORTS, f"close-day-{prev}-status.tsv")
    if not os.path.exists(path):
        return Check(f"close-day {prev}", WARN, "no status file (it did not run)")
    with open(path, encoding="utf-8") as handle:
        rows = [ln.split("\t") for ln in handle.read().splitlines()[1:]]
    # A step that ran again appends a row, so only the newest row of each step counts.
    latest = {s[0]: s for s in rows if len(s) == 4}
    steps = [s for s in latest.values() if s[3] != "0"]
    # A step that exited 1 because its own report says RED ran; its finding was alerted that day.
    reported = [
        s[0] for s in steps if jobs.reported_red("close-day", s[0], s[1], s[3], prev, REPORTS)
    ]
    bad = [s[0] for s in steps if s[0] not in reported]
    if bad:
        # The step id stays in brackets: it is the label the Daily Workflow page shows.
        names = ", ".join(f"{recovery.WORDS.get(step, step)} ({step})" for step in bad)
        warning = f" {recovery.DATA_WARNING}" if recovery.data_open(bad) else ""
        return Check(
            f"close-day {prev}",
            WARN,
            f"these steps did not finish: {names}.{warning} Next: open the Daily Workflow page, "
            "read what the 08:36 recovery did, and run the open steps by hand.",
        )
    if reported:
        return Check(
            f"close-day {prev}", WARN, f"steps {reported} ran and reported RED (see their report)"
        )
    return Check(f"close-day {prev}", OK, "all steps exit 0")


def _calendar_coverage(day: str, calendar=None) -> Check:
    """Whether the NSE calendar file covers today, the next session and the coming year.

    The Python runner cannot warm up on a day the calendar does not cover, and the calendar is
    edited by hand from the NSE circular, so the gap is announced from 1 December.
    """
    from signal_engine.backtest.data_readiness.calendar import (
        SPECIAL,
        TRADING,
        CalendarError,
        load_calendar,
    )

    name = "NSE calendar coverage"
    try:
        calendar = calendar or load_calendar()
        first, last = calendar.coverage
    except CalendarError as exc:
        return Check(name, FAIL, f"calendar file unreadable: {str(exc)[:120]}")

    today = date.fromisoformat(day)
    covered = f"calendar covers {first}-{last}"
    if not first <= today.year <= last:
        return Check(name, FAIL, f"today ({day}) is not covered; {covered}")

    cursor = today
    for _ in range(14):  # a 14-day run with no session has never occurred; bounds the loop
        cursor += timedelta(days=1)
        try:
            kind = calendar.classify(cursor)
        except CalendarError:
            return Check(
                name, FAIL, f"next trading day is not covered (reached {cursor}); {covered}"
            )
        if kind in (TRADING, SPECIAL):
            break

    next_year = today.year + 1
    if today.month == 12 and next_year > last:
        return Check(
            name,
            WARN,
            f"NSE calendar has no {next_year} holidays yet. Add them from the NSE circular "
            f"before the first {next_year} session; the Python runner cannot warm up without "
            f"it. ({covered})",
        )
    return Check(name, OK, f"{covered}; next session {cursor}")


class _HistoryProblem(Exception):
    """The price history cannot be judged. The message is the sentence for the trader."""


def _historify_path() -> str:
    """The store the engine warms up from: HISTORIFY_DB, else db/historify.duckdb (as the engine)."""
    raw = os.environ.get("HISTORIFY_DB") or os.path.join("db", "historify.duckdb")
    return raw if os.path.isabs(raw) else os.path.join(logs.REPO, raw)


def _history_symbols(live: bool | None) -> tuple[list[str], bool, str]:
    """(stocks, every stock in the store too, why nothing is needed or "").

    What the engine warms up at its start: the runner list (the preflight warms it) and the own
    list of every strategy that runs (pipeline/service.py strategy_symbols). In live mode the
    engine drops the paper-only strategies and does not start when none is left, so `live` True
    removes them. An unknown mode (None) keeps every strategy: the safe side.
    """
    from signal_engine import config

    try:
        runner = config._runner_fields(config._load_yaml())["runner"]  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - any failure to read the list is the finding
        logger.exception("open-day: the runner section of config.yaml could not be read")
        raise _HistoryProblem(
            "The list of stocks the engine trades could not be read from signal_engine/config.yaml, "
            "so the price history cannot be checked. Next: fix the runner section of that file, "
            "then run the open-day check again. The reason is in log/errors.jsonl."
        ) from exc
    if not runner.enabled:
        return [], False, "the Python runner is switched off, so no price history is needed"
    running = {t: s for t, s in runner.strategies.items() if not (live and s.paper_only)}
    if not running:
        return [], False, "no Python strategy runs in live mode, so no price history is needed"
    everything = runner.universe == config.UNIVERSE_ALL
    symbols = [] if everything else list(runner.universe)
    for strategy in running.values():
        symbols += list(strategy.universe or ())
    return list(dict.fromkeys(symbols)), everything, ""


def _ist(clock: str) -> float:
    return datetime.fromisoformat(f"{clock}+05:30").timestamp()


def _history_read(path: str, symbols: list[str], everything: bool, previous: str) -> dict:
    """Read-only: per stock, its newest 1-minute bar, and the bars of the previous session.

    Opens the store read-only through backfill._connect_read_only (the helper that the backfill
    verify step uses): it waits out a lock held by another program and raises anything else at
    once. One query for the named stocks; a second for the last bar of a stock with none in the
    look-back window. Never writes.
    """
    import duckdb

    from signal_engine.backtest import backfill

    session_open, continuous_end = _ist(f"{previous}T09:15:00"), _ist(f"{previous}T15:15:00")
    lower = _ist(f"{previous}T00:00:00") - HISTORY_LOOKBACK_DAYS * 86400
    con = backfill._connect_read_only(  # noqa: SLF001
        path, wait=HISTORY_LOCK_WAIT_SECONDS, poll=HISTORY_LOCK_POLL_SECONDS
    )
    timer = threading.Timer(HISTORY_QUERY_SECONDS, con.interrupt)
    timer.start()
    try:
        if everything:  # runner.universe is fno_all: the engine warms every stock the store holds
            stored = con.execute(
                "SELECT DISTINCT symbol FROM market_data WHERE interval = '1m'"
            ).fetchall()
            symbols = list(dict.fromkeys([*symbols, *(r[0] for r in stored)]))
        if not symbols:
            return {"symbols": [], "found": {}}
        rows = con.execute(
            "SELECT symbol, max(timestamp), "
            "count(*) FILTER (WHERE timestamp >= ? AND timestamp < ?), "
            "max(timestamp) FILTER (WHERE timestamp >= ? AND timestamp < ?) "
            "FROM market_data WHERE interval = '1m' AND timestamp >= ? "
            "AND symbol IN (SELECT unnest(?)) GROUP BY symbol",
            [session_open, continuous_end, session_open, continuous_end, lower, symbols],
        ).fetchall()
        found = {r[0]: (r[1], r[2], r[3]) for r in rows}
        for symbol in symbols:
            if symbol not in found:
                last = con.execute(
                    "SELECT max(timestamp) FROM market_data WHERE symbol = ? AND interval = '1m'",
                    [symbol],
                ).fetchone()[0]
                found[symbol] = (last, 0, None)
        return {"symbols": symbols, "found": found}
    except duckdb.InterruptException as exc:
        raise TimeoutError("the read took too long") from exc
    finally:
        timer.cancel()
        con.close()


def _history_judge(previous: str, symbols: list[str], found: dict) -> tuple[list[str], list[str]]:
    """(stale, incomplete): zero tolerance. A stock is fresh when its newest 1-minute bar is on
    the previous trading day or later and that day is a full session, the engine's own test
    (pipeline/context.py HistorifyLoader: MIN_CONTINUOUS_BARS bars before 15:15, the last one at
    or after LAST_CONTINUOUS_BAR). No source of halted or newly listed stocks exists, so none is
    excused."""
    from signal_engine.pipeline.context import LAST_CONTINUOUS_BAR, MIN_CONTINUOUS_BARS

    day_start = _ist(f"{previous}T00:00:00")
    last_wanted = _ist(f"{previous}T{LAST_CONTINUOUS_BAR:%H:%M}:00")
    stale, incomplete = [], []
    for symbol in symbols:
        newest, bars, last_bar = found[symbol]
        if newest is None:
            stale.append(f"{symbol} (no bars at all)")
        elif newest < day_start:
            stale.append(f"{symbol} (last bar {datetime.fromtimestamp(newest, tz=IST):%Y-%m-%d})")
        elif bars < MIN_CONTINUOUS_BARS or last_bar is None or last_bar < last_wanted:
            incomplete.append(f"{symbol} ({bars} bars on {previous})")
    return stale, incomplete


def _history_names(items: list[str]) -> str:
    shown = ", ".join(items[:HISTORY_SHOWN])
    return shown + (f" and {len(items) - HISTORY_SHOWN} more" if len(items) > HISTORY_SHOWN else "")


def _price_history(day: str, live: bool | None = None, *, path: str | None = None) -> Check:
    """The price history the engine warms up from must be current: FAIL (RED) when it is not.

    A separate current-day check. It does not read the close-day status of yesterday, so a failed
    backfill step of yesterday stays a WARN in _yesterday_close when the history of today is good,
    and a successful backfill step never excuses an old store.

    Required set: the stocks the engine warms up today (_history_symbols), 1-minute bars in
    db/historify.duckdb. Fresh: newest bar on the previous trading day (the previous weekday that
    is not an NSE holiday in OpenAlgo's own table) or later, and a full session on that day.
    Tolerance: none. One stale or incomplete stock, a missing or unreadable store, a lock that
    outlasts HISTORY_LOCK_WAIT_SECONDS, or a list of stocks that cannot be read gives FAIL.
    A lock is the one cause that can give a false RED: nothing but the wording tells it apart.
    The store is only ever opened read-only (never written, never opened read-write).
    """
    name = HISTORY_CHECK
    try:
        if not recovery.holiday_table_readable():
            raise _HistoryProblem(
                "The price history could not be checked: the exchange holiday table could not be "
                "read, so the previous trading day is not known. Next: check that db/openalgo.db "
                "opens, then run the open-day check again."
            )
        previous = _previous_close_day(day)
        symbols, everything, unneeded = _history_symbols(live)
        if unneeded:
            return Check(name, OK, unneeded)
        path = path or _historify_path()
        if not os.path.exists(path):
            raise _HistoryProblem(
                f"The price history could not be read: the file {path} does not exist, so the "
                f"engine has nothing to start from. Next: run {BACKFILL_BY_HAND} by hand "
                "(OpenAlgo must be logged in), then restart the engine."
            )
        result = None
        # The lock wait is inside the read. A read that is cut off is tried once more.
        for attempt in (1, 2):
            try:
                result = _history_read(path, symbols, everything, previous)
                break
            except TimeoutError:
                if attempt == 2:
                    raise
        stale, incomplete = _history_judge(previous, result["symbols"], result["found"])
    except _HistoryProblem as problem:
        return Check(name, FAIL, str(problem))
    except TimeoutError:
        logger.exception("open-day: the price history read was cut off twice")
        return Check(
            name,
            FAIL,
            "The price history could not be read: the read took longer than "
            f"{int(HISTORY_QUERY_SECONDS)} seconds twice. Next: run the open-day check again in a few "
            "minutes. If it stays red, check that nothing else is using db/historify.duckdb.",
        )
    except Exception as exc:  # noqa: BLE001 - a lock, a damaged file or a missing table is the finding
        logger.exception("open-day: the price history could not be read")
        if "lock" in str(exc).lower():
            return Check(
                name,
                FAIL,
                "The price history could not be read: another program kept db/historify.duckdb "
                f"open for {int(HISTORY_LOCK_WAIT_SECONDS)} seconds. This can be a false alarm. "
                "Next: run the open-day check again in a few minutes. If it stays red, run "
                f"{BACKFILL_BY_HAND} by hand and restart the engine.",
            )
        return Check(
            name,
            FAIL,
            "The price history could not be read: the file is damaged or has an unexpected "
            "layout. Next: check that db/historify.duckdb opens, then run "
            f"{BACKFILL_BY_HAND} by hand and restart the engine.",
        )
    total = len(result["symbols"])
    if not total:
        return Check(
            name,
            FAIL,
            "The price history could not be used: the store holds no 1-minute bars for any stock "
            f"the engine trades. Next: run {BACKFILL_BY_HAND} by hand (OpenAlgo must be logged "
            "in), then restart the engine.",
        )
    if not (stale or incomplete):
        return Check(
            name,
            OK,
            f"all {total} stocks the engine trades have a full session of {previous} in the store",
        )
    parts = []
    if stale:
        parts.append(
            f"{len(stale)} of {total} stocks have price history older than {previous}: "
            f"{_history_names(stale)}"
        )
    if incomplete:
        parts.append(
            f"{len(incomplete)} of {total} stocks have an incomplete session on {previous}: "
            f"{_history_names(incomplete)}"
        )
    return Check(
        name,
        FAIL,
        "; ".join(parts) + ". The engine leaves these stocks out of today's trading. Next: run "
        f"{BACKFILL_BY_HAND} by hand (OpenAlgo must be logged in), then restart the engine so "
        "that it loads the new history.",
    )


def _late_wake(day: str) -> Check:
    return wakecheck.late_wake_check(day)


def _is_live(checks: list[Check]) -> bool | None:
    """True when the trading-mode check says LIVE, False when it names another mode, else None."""
    for check in checks:
        if check.name == "trading mode":
            return check.evidence.startswith("LIVE")
    return None


def open_day(day: str) -> str:
    mode_checks = _broker_and_mode(day)
    checks = [
        _services(),
        _health(),
        *mode_checks,
        _engine_startup(day),
        _price_history(day, _is_live(mode_checks)),
        _poller_started(day),
        *_ports(),
        _clock(),
        _late_wake(day),
        *_disk_and_databases(day),
        _overnight_errors(day),
        *_morning_tasks(day),
        _yesterday_close(day),
        _calendar_coverage(day),
    ]
    summary = "Is everything ready before the market opens at 09:15?"
    return write_phase("open-day", day, checks, summary)


_STORM = re.compile(
    r"Timeout sending order|ReadTimeout|429|Too Many Requests|Failed to fetch positionbook", re.I
)
_RETRY = re.compile(r"attempt [2-9]/")


def _first_cycle(day: str) -> Check:
    lines = [ln for ln in logs.engine_lines(day, "09:20", "09:40") if "pipeline cycle" in ln]
    if not lines:
        return Check("first runner cycle", FAIL, "no 'pipeline cycle' line 09:20-09:40")
    match = re.search(
        r"evaluated (\d+), fires (\d+), submitted (\d+), skipped (\d+), late (\d+)", lines[0]
    )
    evidence = lines[0].split("|")[-1].strip()[:120]
    if not match:
        return Check("first runner cycle", OK, evidence)
    evaluated, skipped, late = int(match.group(1)), int(match.group(4)), int(match.group(5))
    # A cycle that evaluated nothing and skipped symbols did not trade, even though it logged
    # its summary line. 2026-11-13: every symbol raised, "evaluated 0 ... skipped 40".
    if evaluated == 0 and skipped:
        return Check("first runner cycle", FAIL, evidence)
    return Check("first runner cycle", WARN if (late or skipped) else OK, evidence)


def _first_poll(day: str) -> Check:
    if not os.path.exists(POLLER_LOG):
        return Check("BreakingTrade first scan", FAIL, "no poller log")
    with open(POLLER_LOG, encoding="utf-8", errors="replace") as handle:
        polls = [ln for ln in handle if ln.startswith(day) and " poll 09:2" in ln]
    if not polls:
        return Check("BreakingTrade first scan", FAIL, "no 09:2x poll logged")
    ok = " ok " in polls[0]
    return Check("BreakingTrade first scan", OK if ok else FAIL, polls[0][11:].strip()[:100])


def _storm(day: str, now_hhmm: str) -> list[Check]:
    window = ("09:15", now_hhmm)
    today = logs.engine_lines(day, *window)
    prev = logs.engine_lines(_previous_weekday(day), *window)
    storm_today = sum(1 for ln in today if _STORM.search(ln))
    storm_prev = sum(1 for ln in prev if _STORM.search(ln))
    status = (
        FAIL
        if storm_today >= 10
        else WARN
        if storm_today > 3 and storm_today > 2 * storm_prev
        else OK
    )
    retries = sum(1 for ln in today if _RETRY.search(ln))
    stale = sum(1 for ln in today if "unchanged for" in ln)
    signals = sum(1 for ln in today if "Signal received" in ln or "| sink | dispatch" in ln)
    return [
        Check(
            "timeouts / 429 since the open",
            status,
            f"{storm_today} today vs {storm_prev} yesterday, {window[0]}-{window[1]}",
        ),
        Check("retry loops", WARN if retries >= 5 else OK, f"{retries} retry attempts"),
        Check(
            "stale prices",
            WARN if stale else OK,
            f"{stale} 'unchanged for' warnings (ticks not moving)",
        ),
        Check("signals reaching the pipeline", OK, f"{signals} received or dispatched"),
    ]


def liveness(day: str, now_hhmm: str) -> str:
    checks = [_first_cycle(day), _first_poll(day), *_storm(day, now_hhmm)]
    summary = "Is the engine actually trading in the first minutes after the open?"
    return write_phase("liveness", day, checks, summary)
