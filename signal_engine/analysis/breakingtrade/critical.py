"""Critical BreakingTrade data problems, worded so they cannot be mistaken for routine chatter.

A routine poller message is one line among many. A problem that stops the strategy getting
data (sign-in lost, page reshaped, delivery figure gone, no fresh poll) is sent in a loud
banner, sent once per issue per day (the first occurrence), and followed by a RECOVERED message
when it clears. Plain text only: the banner is capital letters and rules, no icons.
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
        lines.append("(sent once per day; later failures today are in the poller log)")
    lines.append(_RULE)
    return "\n".join(lines)


def _deliver(text: str) -> None:
    from signal_engine.analysis.breakingtrade import alerts

    delivered, message_id = alerts.send(text, "health", monospace=True)
    alerts.record("health", text, deliver=False, delivered=delivered, message_id=message_id)


class CriticalNotifier:
    def __init__(self, deliver=_deliver, clock=datetime.now):
        self._deliver = deliver
        self._clock = clock
        self._last_sent: dict = {}  # key -> date its banner was last sent
        self._open: set = set()  # keys whose banner went out and have not recovered

    def raise_issue(self, key: str, title: str, detail: str, action: str) -> bool:
        """Send the banner on the first occurrence of the day only. True if sent."""
        today = self._clock().date()
        logger.error(f"CRITICAL {title}: {detail}")
        if self._last_sent.get(key) == today:
            return False
        self._last_sent[key] = today
        self._open.add(key)
        try:
            self._deliver(format_critical(title, detail, action))
        except Exception:
            logger.exception("could not send the critical alert")
        return True

    def clear(self, key: str, what: str) -> bool:
        """Say the problem is over, once, if its banner had been sent."""
        if key not in self._open:
            return False
        self._open.discard(key)
        logger.info(f"critical issue cleared: {what}")
        try:
            self._deliver(f"RECOVERED - BREAKINGTRADE DATA: {what}")
        except Exception:
            logger.exception("could not send the recovery notice")
        return True

    def active(self) -> list:
        return sorted(self._open)


notifier = CriticalNotifier()
