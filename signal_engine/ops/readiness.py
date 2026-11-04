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
import urllib.request
from datetime import date, timedelta

from signal_engine.ops import jobs, logs
from signal_engine.ops.model import FAIL, NOT_VERIFIED, OK, REPORTS, WARN, Check, write_phase

HEALTH_URL = "http://127.0.0.1:5000/health/status"
PORTS = {"websocket proxy": 8765, "ZeroMQ bus": 5555}
POLLER_LOG = os.path.join(logs.SE_LOGS, "breakingtrade_poller.log")
DATA = os.path.join(logs.REPO, "signal_engine", "data")
MIN_FREE_GB = 5


def _previous_weekday(day: str) -> str:
    current = date.fromisoformat(day) - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current.isoformat()


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


def _broker_and_mode() -> list[Check]:
    from signal_engine.api_client import fetch_available_capital, fetch_trading_mode

    async def read():
        return await fetch_available_capital(), await fetch_trading_mode()

    try:
        capital, (mode, _is_analyze) = asyncio.run(read())
    except Exception as exc:  # noqa: BLE001
        return [Check("broker session (funds API)", FAIL, f"{type(exc).__name__}: {exc}"[:160])]
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
    for _when, name, message, symbol in records:
        sig = logs.signature(name, message, symbol)
        top[sig] = top.get(sig, 0) + 1
    worst = sorted(top.items(), key=lambda item: -item[1])[:3]
    return Check(
        "errors since the last close",
        WARN,
        f"{len(records)} records; top: " + "; ".join(f"x{n} {s[:70]}" for s, n in worst),
    )


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
    prev = _previous_weekday(day)
    path = os.path.join(REPORTS, f"close-day-{prev}-status.tsv")
    if not os.path.exists(path):
        return Check(f"close-day {prev}", WARN, "no status file (holiday, or it did not run)")
    with open(path, encoding="utf-8") as handle:
        steps = [ln.split("\t") for ln in handle.read().splitlines()[1:]]
    steps = [s for s in steps if len(s) == 4 and s[3] != "0"]
    # A step that exited 1 because its own report says RED ran; its finding was alerted that day.
    reported = [
        s[0] for s in steps if jobs.reported_red("close-day", s[0], s[1], s[3], prev, REPORTS)
    ]
    bad = [s[0] for s in steps if s[0] not in reported]
    if bad:
        return Check(f"close-day {prev}", FAIL, f"failed steps: {bad}")
    if reported:
        return Check(
            f"close-day {prev}", WARN, f"steps {reported} ran and reported RED (see their report)"
        )
    return Check(f"close-day {prev}", OK, "all steps exit 0")


def open_day(day: str) -> str:
    checks = [
        _services(),
        _health(),
        *_broker_and_mode(),
        _engine_startup(day),
        _poller_started(day),
        *_ports(),
        _clock(),
        *_disk_and_databases(day),
        _overnight_errors(day),
        *_morning_tasks(day),
        _yesterday_close(day),
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
    late = int(match.group(5)) if match else 0
    return Check("first runner cycle", WARN if late else OK, lines[0].split("|")[-1].strip()[:120])


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
