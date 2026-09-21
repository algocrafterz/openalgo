"""Daily EOD regression check: trades, signal delivery, and system health — one pass,
with the verdict posted to Telegram.

WHY THIS EXISTS

signal_engine/analysis/eod.sh already produces a markdown report every trading evening
(ledger, declined signals, BreakingTrade review, errors) - but nobody reads a markdown file
unless they remember to. EOD-ANALYSIS-2026-09-11.md and every "review fixes" entry in PRD.md
since exists because a HUMAN sat down and manually cross-checked trades.db against the
broker, grepped the logs for notifier failures, and read the Telegram channels back - the
exact three-part check this module now runs automatically, every day, without anyone
remembering to ask for it.

WHAT IS CHECKED (three categories, same as the manual review this replaces)

  TRADE  - does the day's own record hold together? Every entry has a fill or is flagged;
           no broker fill exists that the engine never sent; nothing is still open at EOD;
           and reconcile.py's canary confirms trades.db's P&L agrees with the broker's own.
  SIGNAL - did the messages this system claims to have sent actually leave? Notifier
           warnings (queue full, flush failure, mirror failure) grepped from today's log;
           BreakingTrade's own delivered/undelivered count from its alerts table; and a
           live (read-only) check that the Telegram session is still authorized and the
           channel OpenAlgo is currently routing to is reachable.
  SYSTEM - anything the trading logic itself didn't cause: config sanity, DB reachability,
           ERROR+ lines in signal_engine's own errors_DAY.jsonl, ERROR+ lines in OpenAlgo's
           own log/errors.jsonl for today, and FD/resource-leak signatures in the day's logs.

DELIVERY: BOT API, NOT THE ENGINE'S TELETHON CLIENT

This runs as a standalone process (cron, or by hand), not inside the engine's event loop.
signal_engine/analysis/breakingtrade/alerts.py already established why that matters: two
processes sharing one Telethon session file is a good way to corrupt it, so anything posted
FROM a separate process uses the Telegram Bot HTTP API instead, with the same
BREAKINGTRADE_BOT_TOKEN the BreakingTrade poller and momentum-rank already share. The read-only
Telegram check below is the one exception - it only ever COPIES the session file, exactly like
tests/test_telegram_integration.py, and never opens the original.

The destination is settings.notify_channel - the SAME admin channel (signal-engine-analyze /
signal-engine-live) the live engine's own day summary already posts to, picked by OpenAlgo's
CURRENT analyze/live mode, so the report always lands in the channel matching what real money
(or paper money) actually did today. The bot must be added as an administrator of both
notify_channel entries once, the same one-time setup alerts.py's docstring describes for its
own channels - if it never was, send_to_admin_channel() logs why and the check still runs.

Usage:
    PYTHONPATH=. uv run python -m signal_engine.analysis.eod_review               # today
    PYTHONPATH=. uv run python -m signal_engine.analysis.eod_review 2026-09-18    # a past day
    PYTHONPATH=. uv run python -m signal_engine.analysis.eod_review --dry-run     # skip Telegram

Wired into signal_engine/analysis/eod.sh, which already runs after the close.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from signal_engine.analysis.breakingtrade import review
from signal_engine.smoke_test import CheckResult, check_config, check_db

_LOG_DIR = "signal_engine/logs"
_REPORTS_DIR = "signal_engine/analysis/reports"
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

#: Substrings that mean a Telegram send was attempted and failed or never happened at all -
#: see notifier.py's notify()/_queue_until_connected()/flush_pending() and
#: _mirror_to_strategy(), which is where every one of these is logged from.
_NOTIFIER_FAILURE_PATTERNS = (
    "Notifier: pending queue full",
    "Notifier: could not flush queued",
    "Notifier: failed to send message",
    "Could not mirror",
)

#: Substrings that mean a resource is leaking, not that a trade went wrong. Kept separate from
#: errors_DAY.jsonl's own count (SYSTEM's other check) because these are actionable signatures
#: worth naming specifically - see the root CLAUDE.md's FD hygiene section for why this class
#: of bug is worth watching for on a single Gunicorn worker that never restarts.
_RESOURCE_LEAK_PATTERNS = (
    "too many open files",
    "Errno 24",
    "OperationalError",
    "database is locked",
    "ResourceWarning",
)


def _time_check(name: str, fn) -> CheckResult:
    t0 = time.monotonic()
    try:
        msg = fn()
        return CheckResult(name=name, passed=True, message=msg, duration_ms=(time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        return CheckResult(name=name, passed=False, message=str(e), duration_ms=(time.monotonic() - t0) * 1000)


async def _atime_check(name: str, coro) -> CheckResult:
    t0 = time.monotonic()
    try:
        msg = await coro
        return CheckResult(name=name, passed=True, message=msg, duration_ms=(time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        return CheckResult(name=name, passed=False, message=str(e), duration_ms=(time.monotonic() - t0) * 1000)


# ── TRADE ────────────────────────────────────────────────────────────────────

def check_trade_ledger(day: str) -> str:
    """Stitch the day's engine events and broker fills, and flag anything a healthy day
    should never show: an order with no fill, a fill with no order, or a position still
    open after the close. Needs eod.sh's --snapshot step to have already run today - a
    missing snapshot means every fill-side check below is silently unable to see anything,
    which is why eod.sh keeps that step first and warns loudly when it fails.
    """
    from signal_engine.analysis.__main__ import load_snapshots
    from signal_engine.analysis.ledger import (
        FLAG_NO_FILL,
        FLAG_OPEN_AT_EOD,
        FLAG_UNMATCHED_FILL,
        build_ledger,
        load_declined,
        load_engine_events,
        load_fills,
    )

    target = datetime.strptime(day, "%Y-%m-%d").date()
    events = load_engine_events(since=target)
    fills = load_fills(load_snapshots())
    positions = build_ledger(events, fills)
    today_positions = [p for p in positions if p.day == target]
    declined = [d for d in load_declined(since=target) if d["_ts"].date() == target]

    no_fill = [p for p in today_positions if FLAG_NO_FILL in p.flags]
    unmatched = [p for p in positions if p.day == target and FLAG_UNMATCHED_FILL in p.flags]
    open_at_eod = [p for p in today_positions if FLAG_OPEN_AT_EOD in p.flags]

    problems = []
    if no_fill:
        problems.append(
            f"{len(no_fill)} entry order(s) sent but never filled: "
            f"{', '.join(p.symbol for p in no_fill)}"
        )
    if unmatched:
        problems.append(
            f"{len(unmatched)} broker fill(s) with no matching engine order: "
            f"{', '.join(p.symbol for p in unmatched)}"
        )
    if open_at_eod:
        problems.append(
            f"{len(open_at_eod)} position(s) still open at EOD: "
            f"{', '.join(p.symbol for p in open_at_eod)}"
        )
    if problems:
        raise RuntimeError("; ".join(problems))

    filled = sum(1 for p in today_positions if p.entry.filled)
    return f"OK — {len(today_positions)} position(s), {filled} filled, {len(declined)} declined"


async def check_reconciliation(mode: str, day: str) -> str:
    """The engine-vs-broker P&L canary (reconcile.py), for the mode the engine actually ran
    under today. Never compares across modes: OpenAlgo's own funds API answers for whichever
    engine (sandbox or live) it is CURRENTLY pointed at, so this only means anything when
    `mode` matches that - the same assumption tracker.py's own end-of-day call already makes.
    """
    from signal_engine import reconcile

    result = await reconcile.reconcile_day(mode, day)
    if not result.comparable:
        return f"SKIPPED — {result.summary()}"
    if result.is_known_sandbox_limitation:
        # See Reconciliation.is_known_sandbox_limitation (reconcile.py): confirmed
        # 2026-09-21 this is OpenAlgo's own sandbox undercounting multi-leg closes, not a
        # signal_engine defect - trades.db's total_pnl is independently correct. Informational
        # in the EOD report, not a [FAIL] - the trader cannot act on a sandbox-side limitation
        # and it is out of scope for signal_engine to fix (OpenAlgo core).
        return f"SKIPPED (known OpenAlgo sandbox limitation, not an engine defect) — {result.summary()}"
    if not result.agrees:
        raise RuntimeError(result.summary())
    return result.summary()


# ── SIGNAL ───────────────────────────────────────────────────────────────────

def check_notifier_delivery(day: str) -> str:
    """Grep the day's engine logs for a notifier send that was attempted and did not land.

    Scans all three mode logs (live/analyze/unknown) rather than trusting a freshly-queried
    "current mode" - that can read differently by the time this runs than it did all day,
    and the log files themselves are the ground truth for what actually happened.
    """
    hits: list[str] = []
    for mode in ("live", "analyze", "unknown"):
        path = os.path.join(_LOG_DIR, f"signal_engine_{mode}_{day}.log")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if any(p in line for p in _NOTIFIER_FAILURE_PATTERNS):
                    hits.append(line.strip())
    if hits:
        raise RuntimeError(f"{len(hits)} notifier delivery issue(s) logged, e.g.: {hits[0][:160]}")
    return "OK — no notifier delivery failures logged"


def check_breakingtrade_alert_delivery(day: str) -> str:
    """BreakingTrade's own alerts table already records delivered=0/1 per alert (see
    alerts.py's send()) - this is the one strategy family where "was it actually sent" has
    a direct answer in the data instead of needing a log grep.
    """
    from signal_engine.analysis.breakingtrade import store

    if not os.path.exists(store._DB_PATH):
        return "OK — no breakingtrade.db yet"
    conn = sqlite3.connect(store._DB_PATH)
    try:
        total, delivered = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(delivered), 0) FROM alerts WHERE date(created_at) = ?",
            (day,),
        ).fetchone()
    except sqlite3.OperationalError:
        return "OK — no alerts table yet"
    finally:
        conn.close()
    if total == 0:
        return "OK — no BreakingTrade alerts today"
    if delivered < total:
        raise RuntimeError(
            f"{total - delivered}/{total} BreakingTrade alert(s) recorded but not delivered"
        )
    return f"OK — {delivered}/{total} BreakingTrade alerts delivered"


async def check_telegram_channels_live(phase: str) -> str:
    """Read-only: the session is still authorized, and the admin channel OpenAlgo is
    CURRENTLY routing to (notify_channel[phase]) is reachable.

    Opens a COPY of the live session, never the original - see
    tests/test_telegram_integration.py's docstring for why sharing the file with whatever
    process is currently running the engine corrupts it.
    """
    import shutil
    import tempfile

    from telethon import TelegramClient

    from signal_engine.config import settings

    session_path = os.path.join(os.path.dirname(__file__), "..", "data", "telegram.session")
    if not (settings.telegram_api_id and settings.telegram_api_hash):
        return "SKIPPED — Telegram credentials not configured"
    if not os.path.exists(session_path):
        return "SKIPPED — no telegram.session file yet"

    handle, tmp_path = tempfile.mkstemp(prefix="telegram_eod_", suffix=".session")
    os.close(handle)
    shutil.copyfile(session_path, tmp_path)
    client = TelegramClient(tmp_path[: -len(".session")], settings.telegram_api_id, settings.telegram_api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session expired — re-authenticate the engine")
        channel = settings.notify_channel.get(phase)
        if channel is None:
            return f"OK — session authorized (no notify_channel configured for {phase})"
        entity = await client.get_entity(channel.id)
        if entity is None:
            raise RuntimeError(f"cannot access {channel.name} ({channel.id})")
        return f"OK — session authorized, {channel.name} reachable"
    finally:
        await client.disconnect()
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# ── SYSTEM ───────────────────────────────────────────────────────────────────

def check_signal_engine_errors(day: str) -> str:
    """signal_engine's own errors_DAY.jsonl - see logger_setup.py's _error_jsonl_sink."""
    path = os.path.join(_LOG_DIR, f"errors_{day}.jsonl")
    rows = _read_jsonl(path)
    if not rows:
        return "OK — no signal_engine errors logged"
    critical = sum(1 for r in rows if r.get("level") == "CRITICAL")
    sample = str(rows[0].get("message", ""))[:160]
    raise RuntimeError(f"{len(rows)} error(s) logged ({critical} CRITICAL), e.g.: {sample}")


def check_openalgo_app_errors(day: str) -> str:
    """OpenAlgo's own log/errors.jsonl - the root CLAUDE.md's documented first place to look
    when debugging, filtered to today since the file itself is a rolling last-1000-lines
    buffer with no per-day rotation.
    """
    rows = [r for r in _read_jsonl(os.path.join("log", "errors.jsonl")) if str(r.get("ts", "")).startswith(day)]
    if not rows:
        return "OK — no OpenAlgo app errors logged today"
    sample = str(rows[0].get("message", ""))[:160]
    raise RuntimeError(f"{len(rows)} OpenAlgo app error(s) logged today, e.g.: {sample}")


def check_resource_warnings(day: str) -> str:
    """FD/DB-lock signatures in today's logs - see the root CLAUDE.md's FD hygiene section."""
    hits = 0
    example = ""
    for mode in ("live", "analyze", "unknown"):
        path = os.path.join(_LOG_DIR, f"signal_engine_{mode}_{day}.log")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if any(p in line for p in _RESOURCE_LEAK_PATTERNS):
                    hits += 1
                    example = example or line.strip()[:160]
    if hits:
        raise RuntimeError(f"{hits} resource/FD warning(s) logged, e.g.: {example}")
    return "OK — no resource/FD warnings logged"


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# ── Report ───────────────────────────────────────────────────────────────────

_CATEGORIES = ("TRADE", "SIGNAL", "SYSTEM")


@dataclass
class EODReport:
    day: str
    mode: str
    phase: str
    checks: list[tuple[str, CheckResult]] = field(default_factory=list)

    def add(self, category: str, result: CheckResult) -> None:
        self.checks.append((category, result))

    def _rows(self, category: str) -> list[CheckResult]:
        return [r for c, r in self.checks if c == category]

    @property
    def all_passed(self) -> bool:
        return all(r.passed for _, r in self.checks)

    @property
    def pass_count(self) -> int:
        return sum(1 for _, r in self.checks if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for _, r in self.checks if not r.passed)

    def print(self) -> None:
        print()
        print("=" * 72)
        print(f"  Signal Engine EOD Regression Check — {self.day} ({self.phase.upper()}, mode={self.mode})")
        print("=" * 72)
        for category in _CATEGORIES:
            rows = self._rows(category)
            if not rows:
                continue
            print(f"\n[{category}]")
            for r in rows:
                print(str(r))
        print("-" * 72)
        status = "ALL CHECKS PASSED" if self.all_passed else f"{self.fail_count} CHECK(S) FAILED"
        print(f"  Result: {status} ({self.pass_count}/{len(self.checks)})")
        print("=" * 72)
        print()

    def telegram_digest(self, max_len: int = 3800) -> str:
        lines = [f"EOD REGRESSION CHECK — {self.day} ({self.phase.upper()})", ""]
        for category in _CATEGORIES:
            rows = self._rows(category)
            if not rows:
                continue
            lines.append(f"[{category}]")
            for r in rows:
                icon = "PASS" if r.passed else "FAIL"
                lines.append(f"  [{icon}] {r.name}: {r.message}")
            lines.append("")
        status = "ALL CHECKS PASSED" if self.all_passed else f"{self.fail_count} CHECK(S) FAILED"
        lines.append(f"Result: {status} ({self.pass_count}/{len(self.checks)})")
        lines.append(f"Full report: {_REPORTS_DIR}/eod-{self.day}.md")
        text = "\n".join(lines)
        if len(text) > max_len:
            text = text[: max_len - 20].rstrip() + "\n... (truncated)"
        return text


async def run_eod_review(day: str) -> EODReport:
    mode, is_analyze = review.trading_mode()
    # Same rule as alerts.py's _current_phase(): unreachable/unknown defaults to the
    # lower-stakes destination rather than guessing "live".
    phase = "analyze" if mode == "unknown" or is_analyze else "live"

    report = EODReport(day=day, mode=mode, phase=phase)

    report.add("TRADE", _time_check("Trade ledger integrity", lambda: check_trade_ledger(day)))
    report.add("TRADE", await _atime_check("Trade — engine vs broker P&L", check_reconciliation(mode, day)))

    report.add("SIGNAL", _time_check("Signal — notifier delivery", lambda: check_notifier_delivery(day)))
    report.add("SIGNAL", _time_check(
        "Signal — BreakingTrade alert delivery", lambda: check_breakingtrade_alert_delivery(day)
    ))
    report.add("SIGNAL", await _atime_check(
        "Signal — Telegram channel reachable", check_telegram_channels_live(phase)
    ))

    report.add("SYSTEM", _time_check("System — config", check_config))
    report.add("SYSTEM", _time_check("System — risk DB accessible", check_db))
    report.add("SYSTEM", _time_check(
        "System — signal_engine errors today", lambda: check_signal_engine_errors(day)
    ))
    report.add("SYSTEM", _time_check(
        "System — OpenAlgo app errors today", lambda: check_openalgo_app_errors(day)
    ))
    report.add("SYSTEM", _time_check("System — resource/FD warnings", lambda: check_resource_warnings(day)))

    return report


# ── Telegram delivery ────────────────────────────────────────────────────────

def send_to_admin_channel(text: str, phase: str) -> bool:
    """Post `text` to settings.notify_channel[phase] via the Telegram Bot HTTP API - see the
    module docstring for why a bot, not the engine's own Telethon client. Never raises.
    """
    from signal_engine.analysis.breakingtrade.alerts import _clean_token, _env
    from signal_engine.config import settings

    token = _clean_token(_env().get("BREAKINGTRADE_BOT_TOKEN"))
    channel = settings.notify_channel.get(phase) or next(iter(settings.notify_channel.values()), None)
    if not token or channel is None:
        print("  [eod_review] BREAKINGTRADE_BOT_TOKEN or notify_channel missing — report not delivered")
        return False
    try:
        response = httpx.post(
            _TELEGRAM_API.format(token=token),
            json={
                "chat_id": str(channel.id),
                "text": f"```\n{text}\n```",
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if response.status_code == 200:
            return True
        print(f"  [eod_review] Telegram send failed: HTTP {response.status_code} {response.text[:200]}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [eod_review] Telegram send failed: {e}")
        return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(prog="signal_engine.analysis.eod_review")
    ap.add_argument("day", nargs="?", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--dry-run", action="store_true", help="print the report, do not post to Telegram")
    args = ap.parse_args()

    day = args.day or date.today().isoformat()
    report = asyncio.run(run_eod_review(day))
    report.print()

    digest = report.telegram_digest()
    if args.dry_run:
        print("[dry-run] would send to Telegram:\n")
        print(digest)
    else:
        delivered = send_to_admin_channel(digest, report.phase)
        print(f"Telegram delivery: {'sent' if delivered else 'NOT sent (see above)'}")

    raise SystemExit(0 if report.all_passed else 1)


if __name__ == "__main__":
    main()
