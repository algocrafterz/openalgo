"""Loguru logging configuration with console and file sinks."""

import os
import sys

from loguru import logger

_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {module} | {message}"


def setup_logger() -> logger.__class__:
    # File sink defaults to INFO (keeps logs readable).
    # Set SIGNAL_ENGINE_LOG_LEVEL=DEBUG to capture poll-level debug noise when diagnosing.
    file_level = os.getenv("SIGNAL_ENGINE_LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level="INFO", format=_LOG_FORMAT)
    logger.add(
        "signal_engine/logs/signal_engine_{time:YYYY-MM-DD}.log",
        level=file_level,
        format=_LOG_FORMAT,
        rotation="1 day",
        retention="30 days",
    )
    return logger
