"""Critical BreakingTrade data problems, worded so they cannot be mistaken for routine chatter.

A routine poller message is one line among many. A problem that stops the strategy getting
data (sign-in lost, page reshaped, delivery figure gone, no fresh poll) is recorded as a loud
banner, once per issue per day (the first occurrence), and followed by a RECOVERED record
when it clears. Plain text only: the banner is capital letters and rules, no icons.

Nothing is sent to Telegram (outgoing notifications were removed in November 2026). Each
banner is logged at ERROR and recorded as a `health` row in the alerts table, never posted:
strategy channels carry trade signals only (see alerts._SIGNAL_KINDS).
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger

_RULE = "!" * 34


def format_critical(title: str, detail: str, action: str, repeats: bool = True) -> str:
    lines = [
        _RULE,
        "CRITICAL - BREAKINGTRADE DATA",
        _RULE,
        title.upper(),
        "",
        detail.strip(),
        "",
        f"ACTION NEEDED: {action.strip()}",
    ]
    if repeats:
        lines.append("(recorded once per day; later failures today are in the poller log)")
    lines.append(_RULE)
    return "\n".join(lines)


def _record(text: str) -> None:
    from signal_engine.analysis.breakingtrade import alerts

    alerts.record("health", text, deliver=False)


class CriticalIssues:
    def __init__(self, record=_record, clock=datetime.now):
        self._record = record
        self._clock = clock
        self._last_sent: dict = {}  # key -> date its banner was last recorded
        self._open: set = set()  # keys whose banner was recorded and have not recovered

    def raise_issue(self, key: str, title: str, detail: str, action: str) -> bool:
        """Record the banner on the first occurrence of the day only. True if recorded."""
        today = self._clock().date()
        logger.error(f"CRITICAL {title}: {detail}")
        if self._last_sent.get(key) == today:
            return False
        self._last_sent[key] = today
        self._open.add(key)
        try:
            self._record(format_critical(title, detail, action))
        except Exception:
            logger.exception("could not record the critical issue")
        return True

    def clear(self, key: str, what: str) -> bool:
        """Record that the problem is over, once, if its banner had been recorded."""
        if key not in self._open:
            return False
        self._open.discard(key)
        logger.info(f"critical issue cleared: {what}")
        try:
            self._record(f"RECOVERED - BREAKINGTRADE DATA: {what}")
        except Exception:
            logger.exception("could not record the recovery")
        return True

    def active(self) -> list:
        return sorted(self._open)


issues = CriticalIssues()
