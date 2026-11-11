"""Critical BreakingTrade data problems, worded so they cannot be mistaken for routine chatter.

A routine poller message is one line among many. A problem that stops the strategy getting
data (sign-in lost, page reshaped, delivery figure gone, no fresh poll) is sent in a loud
banner, repeated every REPEAT_MINUTES until it is cleared, and followed by a RECOVERED message
when it is. Plain text only: the banner is capital letters and rules, no icons.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

REPEAT_MINUTES = 30
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
        lines.append(f"(repeats every {REPEAT_MINUTES} min until fixed)")
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
        self._last_sent: dict = {}

    def raise_issue(self, key: str, title: str, detail: str, action: str) -> bool:
        """Send the banner now, or again when the repeat interval has passed. True if sent."""
        now = self._clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < timedelta(minutes=REPEAT_MINUTES):
            return False
        self._last_sent[key] = now
        logger.error(f"CRITICAL {title}: {detail}")
        try:
            self._deliver(format_critical(title, detail, action))
        except Exception:
            logger.exception("could not send the critical alert")
        return True

    def clear(self, key: str, what: str) -> bool:
        """Say the problem is over, once, if it had been reported."""
        if self._last_sent.pop(key, None) is None:
            return False
        logger.info(f"critical issue cleared: {what}")
        try:
            self._deliver(f"RECOVERED - BREAKINGTRADE DATA: {what}")
        except Exception:
            logger.exception("could not send the recovery notice")
        return True

    def active(self) -> list:
        return sorted(self._last_sent)


notifier = CriticalNotifier()
