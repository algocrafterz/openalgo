"""Loguru logging configuration with console and file sinks."""

import sys

from loguru import logger

_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {module} | {message}"


def setup_logger() -> logger.__class__:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format=_LOG_FORMAT)
    logger.add(
        "signal_engine/logs/signal_engine_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format=_LOG_FORMAT,
        rotation="1 day",
        retention="30 days",
    )
    return logger
