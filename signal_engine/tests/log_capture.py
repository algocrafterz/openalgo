"""Capture loguru lines in a test.

Outgoing Telegram notifications were removed in November 2026. A failure that once sent an
alert is now a log line, so a test that pinned "the trader is told" now pins "the line is
logged". Use the `logged` fixture (conftest) or `capture(stack)` inside an ExitStack harness.
"""

from __future__ import annotations

import contextlib

from loguru import logger

_LEVELS = {"DEBUG": 10, "INFO": 20, "SUCCESS": 25, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class Logged(list):
    """(level number, level name, message) tuples, in order."""

    def messages(self, level: str = "WARNING") -> list[str]:
        floor = _LEVELS[level]
        return [msg for no, _name, msg in self if no >= floor]

    def has(self, text: str, level: str = "WARNING") -> bool:
        return any(text in msg for msg in self.messages(level))

    def count(self, text: str, level: str = "WARNING") -> int:  # type: ignore[override]
        return sum(text in msg for msg in self.messages(level))

    def text(self, level: str = "WARNING") -> str:
        """Every line at `level` or above, joined, for substring checks."""
        return "\n".join(self.messages(level))


@contextlib.contextmanager
def capturing(found: Logged | None = None):
    """Capture into `found` (a new Logged when None) while the block runs."""
    if found is None:
        found = Logged()
    sink = logger.add(
        lambda m: found.append((m.record["level"].no, m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    try:
        yield found
    finally:
        logger.remove(sink)


def capture(stack: contextlib.ExitStack) -> Logged:
    """Start capturing for the life of `stack`."""
    return stack.enter_context(capturing())
