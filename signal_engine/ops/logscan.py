"""Close-day step `logs`: today's errors grouped by signature, against the previous 5 sessions.

Owns error analysis for the day; the review reads its output and never re-counts. NEW = not seen
in the previous 5 sessions; RISING = more than twice the previous daily average and at least 5.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import date, timedelta

from signal_engine.bugs import store
from signal_engine.ops import logs
from signal_engine.ops.model import FAIL, OK, WARN, Check, write_phase

#: Signatures that touch order flow or a known failure mode get named in the report.
FLAGS = {
    "rate limit": re.compile(r"429|too many requests|rate.?limit", re.I),
    "timeout": re.compile(r"timeout|timed out|readtimeout", re.I),
    "reconnect": re.compile(r"connection to remote host was lost|reconnect|disconnected", re.I),
    "db locked": re.compile(r"database is locked", re.I),
    "fd exhaustion": re.compile(r"too many open files", re.I),
    "token": re.compile(r"token (expired|invalid)|session expired|invalid session", re.I),
    "greenlet/lock": re.compile(
        r"greenlet|cannot switch to a different thread|has no attribute 'lock'", re.I
    ),
    "order rejected": re.compile(r"reject", re.I),
    "unconfirmed exit": re.compile(r"exit uncertain|unconfirmed|tracking blind", re.I),
}


def _sessions_before(day: str, count: int) -> list[str]:
    """The `count` weekdays before `day` (holidays read as empty days, which only lowers a baseline)."""
    current, found = date.fromisoformat(day), []
    while len(found) < count:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            found.append(current.isoformat())
    return found


def _counts(day: str) -> tuple[Counter, dict]:
    counts, first = Counter(), {}
    for source in (logs.engine_errors(day), logs.app_errors(day)):
        for when, logger_name, message, symbol in source:
            sig = logs.signature(logger_name, message, symbol)
            counts[sig] += 1
            first.setdefault(sig, when)
    return counts, first


def classify(today: Counter, history: list[Counter]) -> dict[str, str]:
    """NEW / RISING / recurring for each of today's signatures."""
    result = {}
    for sig, n in today.items():
        before = [h.get(sig, 0) for h in history]
        if not any(before):
            result[sig] = "NEW"
        elif n >= 5 and n > 2 * (sum(before) / len(before)):
            result[sig] = "RISING"
        else:
            result[sig] = "recurring"
    return result


def run(day: str) -> str:
    today, first = _counts(day)
    history = [_counts(d)[0] for d in _sessions_before(day, 5)]
    kinds = classify(today, history)
    rows = sorted(today.items(), key=lambda item: (kinds[item[0]] == "recurring", -item[1]))
    checks = []
    for sig, n in rows[:40]:
        flags = [name for name, pattern in FLAGS.items() if pattern.search(sig)]
        status = WARN if kinds[sig] in ("NEW", "RISING") else OK
        if kinds[sig] != "recurring" and {"fd exhaustion", "greenlet/lock", "db locked"} & set(
            flags
        ):
            status = FAIL
        checks.append(
            Check(
                f"{kinds[sig]} x{n}",
                status,
                f"first {first[sig]}; {sig}" + (f" [{', '.join(flags)}]" if flags else ""),
            )
        )
    new = sum(1 for k in kinds.values() if k == "NEW")
    rising = sum(1 for k in kinds.values() if k == "RISING")
    summary = (
        f"{sum(today.values())} errors today, of {len(today)} kinds; {new} new and {rising} "
        "happening more than usual, compared with the previous 5 trading days."
    )
    return write_phase(
        "logs", day, checks or [Check("errors", OK, "no error records today")], summary
    )


# --- R3.2: conservative detector turning recurring errors into MEDIUM bugs -----------------------

#: Known background noise. Never turned into a bug, however often it repeats.
BENIGN = re.compile(
    r"connection to remote host was lost|ping/pong timed out|no market data for"
    r"|failed download|\b404\b|NIFTYFPI|possibly delisted|quoteSummary",
    re.I,
)
MIN_HITS = 5
MIN_DAYS = 2
_SECRET = re.compile(
    r"(token|secret|password|passwd|apikey|api_key|authorization|bearer)\S*\s*[=:]?\s*"
    r"(?:(?:bearer|basic)\s+)?\S+"
    r"|[A-Za-z0-9_\-]{24,}",
    re.I,
)


def source_key(sig: str) -> str:
    return "log-signature:" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:10]


def mask_evidence(sig: str, hits: int, days: int) -> str:
    """One masked line, at most 200 characters: no tokens, no long ids."""
    line = _SECRET.sub("<masked>", sig.splitlines()[0] if sig else "")
    return f"Log detector: {line} ({hits} today, seen on {days} day(s))"[:200]


TITLE_PREFIX = "Recurring log error: "


def mask_title(sig: str) -> str:
    """Bug title for a signature, masked like the evidence line."""
    line = _SECRET.sub("<masked>", sig.splitlines()[0] if sig else "")
    return f"{TITLE_PREFIX}{line[:90]}"


def candidates(day: str, counter=None, sessions: int = 5) -> list[tuple[str, int, int]]:
    """(signature, hits today, days seen incl. today) for signatures passing the candidate rule."""
    counter = counter or (lambda d: _counts(d)[0])
    today = counter(day)
    history = [counter(d) for d in _sessions_before(day, sessions)]
    found = []
    for sig, hits in today.items():
        if BENIGN.search(sig):
            continue
        days = 1 + sum(1 for h in history if h.get(sig, 0) > 0)
        if hits >= MIN_HITS or days >= MIN_DAYS:
            found.append((sig, hits, days))
    return sorted(found, key=lambda item: (-item[1], item[0]))


#: A placeholder already written by normalize (kept as is), or a ticker-shaped word.
_TICKER = re.compile(
    r"<(?:SYM|ID|TOKEN)>|(?<![A-Za-z0-9_&-])[A-Z][A-Z0-9&-]{2,19}(?![A-Za-z0-9_&-])"
)
#: A two-letter upper-case word is a ticker (LT, M&M's short cousins) only in an explicit symbol
#: context: after "symbol", "ticker", "instrument", "order for", "position in", or "for XX"
#: followed by an order or trade word. Common two-letter tokens never count.
_SHORT_AFTER_LABEL = re.compile(
    r"\b(?i:symbol|ticker|instrument|order for|position in)(?:\s*[=:]\s*|\s+)"
    r"([A-Z]{2})(?![A-Za-z0-9_&<-])"
)
_SHORT_BEFORE_TRADE_WORD = re.compile(
    r"\bfor\s+([A-Z]{2})(?=\s+(?i:order|trade|bar|position|entry|exit|stop|signal|fill)\b)"
)
_SHORT_STOP = frozenset(
    "OK ID IP IO DB UI OS NO IF TO ON AT BY OR AM PM US UK IN IS IT OF AS AN BE DO GO HE ME MY SO UP "
    "WE AI CI CD QA TV PC EU UN EN HR PR VM FS FD RC MQ WS".split()
)
_HEXID = re.compile(
    r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b|\b[0-9a-f]{8}-[0-9a-f-]{27}\b"
)
_LONGTOKEN = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
#: Upper-case words that are log levels, protocols or jargon, never a ticker.
_NOT_TICKERS = frozenset(
    "ERROR WARN WARNING INFO DEBUG CRITICAL FATAL NONE NULL TRUE FALSE NAN API HTTP HTTPS URL JSON SQL "
    "GET POST PUT DELETE NSE NFO MCX CDS TCP SSL TLS WS LTP OHLC OHLCV SL TP PNL MIS CNC NRML LIMIT "
    "MARKET BUY SELL OPEN CLOSED PENDING REJECTED COMPLETE CANCELLED UTC IST EOD".split()
)


def symbol_universe(day: str, sessions: int = 5) -> set[str]:
    """Every record symbol seen in the day's logs and the previous sessions (best effort)."""
    found: set[str] = set()
    for d in [day, *_sessions_before(day, sessions)]:
        try:
            for source in (logs.engine_errors(d), logs.app_errors(d)):
                found.update(sym for *_, sym in source if sym and sym != "-")
        except OSError:
            continue
    return found


def normalize(sig: str, universe=frozenset()) -> str:
    """Collapse per-symbol and per-id variants: keep the logger and the message structure.

    Tickers (known symbols, or conservative upper-case words that are not log jargon), hex ids and
    long tokens become placeholders. Digits are already '#' from logs.signature().
    """
    head, sep, message = sig.partition(": ")
    if not sep:
        head, message = "", sig
    message = _HEXID.sub("<ID>", message)
    message = _LONGTOKEN.sub("<TOKEN>", message)

    def ticker(match: re.Match) -> str:
        word = match.group(0)
        if word.startswith("<"):
            return word
        return "<SYM>" if word in universe or word not in _NOT_TICKERS else word

    def short(match: re.Match) -> str:
        word = match.group(1)
        if word in _SHORT_STOP:
            return match.group(0)
        return match.group(0)[: match.start(1) - match.start(0)] + "<SYM>"

    message = _TICKER.sub(ticker, message)
    message = _SHORT_AFTER_LABEL.sub(short, message)
    message = _SHORT_BEFORE_TRADE_WORD.sub(short, message)
    if universe:
        names = sorted((u for u in universe if len(u) < 3), key=len, reverse=True)
        for name in names:
            message = re.sub(
                rf"(?<![A-Za-z0-9_&-]){re.escape(name)}(?![A-Za-z0-9_&-])", "<SYM>", message
            )
    return f"{head}: {message}" if sep else message


def _siblings(sigs: list[str]) -> set[str]:
    """Short or unknown tickers inferred from the data: one upper-case word that is the only thing
    differing across 3 or more otherwise identical signatures (e.g. 'pipeline LT bar #')."""
    slots: dict[tuple, set[str]] = {}
    for sig in sigs:
        words = sig.split(" ")
        for i, word in enumerate(words):
            if re.fullmatch(r"[A-Z][A-Z0-9&-]{1,19}", word):
                slots.setdefault((i, *words[:i], None, *words[i + 1 :]), set()).add(word)
    return {w for group in slots.values() if len(group) >= 3 for w in group}


def _normalized(counter, universe):
    """Wrap a per-day signature counter so equal normalized signatures add up."""

    def wrapped(day: str) -> Counter:
        # BENIGN noise is judged on the raw text too: normalizing can rewrite the very words
        # (NIFTYFPI.NS, tickers) a benign pattern names.
        raw = Counter({sig: n for sig, n in counter(day).items() if not BENIGN.search(sig)})
        known = set(universe) | _siblings(list(raw))
        merged: Counter = Counter()
        for sig, n in raw.items():
            merged[normalize(sig, known)] += n
        return merged

    return wrapped


def detect(day: str, dry_run: bool = True, *, counter=None, path: str | None = None) -> list[str]:
    """Record recurring non-benign errors in the bug tracker (MEDIUM, open). Dry-run by default.

    A match to an existing bug adds an occurrence through store.report, which never closes,
    downgrades or verifies. Returns one line per candidate.
    """
    lines = []
    universe = symbol_universe(day) if counter is None else set()
    counter = _normalized(counter or (lambda d: _counts(d)[0]), universe)
    try:
        known = {s: b["id"] for b in store.load(path)["bugs"] for s in b.get("sources", [])}
    except (store.TrackerError, ValueError, OSError) as exc:
        return [f"skipped: bug tracker unreadable: {exc}"]
    for sig, hits, days in candidates(day, counter):
        key = source_key(sig)
        existing = known.get(key)
        if dry_run:
            verb = f"would update {existing}" if existing else "would create"
            lines.append(
                f"[dry-run] {verb}: {key} x{hits} on {days} day(s): {mask_title(sig)[len(TITLE_PREFIX) :][:80]}"
            )
            continue
        try:
            result = store.report(
                source_key=key,
                title=mask_title(sig),
                kind="EXECUTION",
                severity="MEDIUM",
                area="ops",
                day=day,
                impact="Recorded automatically from a recurring log error. Not reviewed.",
                evidence=[mask_evidence(sig, hits, days)],
                sources=[key],
                plain="An error keeps repeating in the logs; the owner should look at it.",
                path=path,
            )
        except store.TrackerError as exc:
            lines.append(f"skipped {key}: {exc}")
            continue
        bug = result["bug"]["id"]
        verb = (
            "created" if result["created"] else ("updated" if result["recorded"] else "unchanged")
        )
        lines.append(f"{verb} {bug}: {key} x{hits}")
    return lines
