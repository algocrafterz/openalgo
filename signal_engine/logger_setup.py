"""Loguru logging configuration with console and file sinks."""

import os
import sys

from loguru import logger

# The symbol column is what makes a trading day greppable: every line emitted while
# handling a signal, or while polling a tracked position, carries the symbol it
# concerns. Lines with no symbol context (startup, config, day summary) show "-".
_LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {extra[symbol]: <12} | {module} | {message}"
)


def setup_logger() -> logger.__class__:
    # File sink defaults to INFO (keeps logs readable).
    # Set SIGNAL_ENGINE_LOG_LEVEL=DEBUG to capture poll-level debug noise when diagnosing.
    file_level = os.getenv("SIGNAL_ENGINE_LOG_LEVEL", "INFO").upper()
    logger.remove()
    # Default for the symbol column so lines logged outside any symbol context still format.
    logger.configure(extra={"symbol": "-"})
    logger.add(sys.stderr, level="INFO", format=_LOG_FORMAT)
    logger.add(
        "signal_engine/logs/signal_engine_{time:YYYY-MM-DD}.log",
        level=file_level,
        format=_LOG_FORMAT,
        rotation="1 day",
        retention="30 days",
        # Frames that produced the error, not just the raising line. `diagnose` stays OFF on
        # purpose: it renders local variable VALUES into the log, and the locals around an
        # order call hold the API key and the broker session token.
        backtrace=True,
        diagnose=False,
    )
    # Errors only, one JSON object per line. The full log above is a whole trading day of
    # polls and fills; after a bad session the first question is "what broke", and that should
    # be a short file, not a grep. Mirrors the house convention in the root CLAUDE.md, where
    # log/errors.jsonl is the documented first place to look when debugging.
    logger.add(
        "signal_engine/logs/errors_{time:YYYY-MM-DD}.jsonl",
        level="ERROR",
        rotation="1 day",
        retention="90 days",
        serialize=True,
        backtrace=True,
        diagnose=False,
    )
    return logger
