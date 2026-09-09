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

#: Which mode's log file new lines route to. "unknown" until startup.py resolves the real
#: mode (fetch_trading_mode() runs after config/DB init, so a startup window genuinely has no
#: mode yet - see set_mode()'s docstring). A separate module-level flag rather than reading
#: db._TRADE_MODE, since logger_setup is imported very early and needs to stay a leaf module.
_current_mode = "unknown"


def set_mode(mode: str) -> None:
    """Route subsequent log lines to that mode's own file.

    2026-09-10: analyze (paper) and live trading used to share one signal_engine_{date}.log,
    which meant a day running paper strategies alongside a live one interleaved both in the
    same file - exactly the "which rows are comparable" problem trades.db's trade_mode column
    already exists to solve, just for logs instead of trade rows. Call this from the SAME call
    site as db.set_trade_mode() (startup.py, right after fetch_trading_mode() resolves the
    mode) so both stay in sync.

    Uses a loguru `filter` evaluated per log LINE, not per sink creation, so a mode learned
    after some lines were already written doesn't need the sinks rebuilt - anything logged
    before the mode is known lands in signal_engine_unknown_{date}.log, which should be a very
    short, rarely-read file (just the startup window before the mode resolves).
    """
    global _current_mode
    _current_mode = (mode or "unknown").strip().lower() or "unknown"


def setup_logger() -> logger.__class__:
    # File sink defaults to INFO (keeps logs readable).
    # Set SIGNAL_ENGINE_LOG_LEVEL=DEBUG to capture poll-level debug noise when diagnosing.
    file_level = os.getenv("SIGNAL_ENGINE_LOG_LEVEL", "INFO").upper()
    logger.remove()
    # Default for the symbol column so lines logged outside any symbol context still format.
    logger.configure(extra={"symbol": "-"})
    logger.add(sys.stderr, level="INFO", format=_LOG_FORMAT)

    _file_kwargs = dict(
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
    logger.add(
        "signal_engine/logs/signal_engine_live_{time:YYYY-MM-DD}.log",
        filter=lambda record: _current_mode == "live",
        **_file_kwargs,
    )
    logger.add(
        "signal_engine/logs/signal_engine_analyze_{time:YYYY-MM-DD}.log",
        filter=lambda record: _current_mode == "analyze",
        **_file_kwargs,
    )
    # Startup window before fetch_trading_mode() resolves which mode this session is - and the
    # fallback if it never does (a crash before that point). Should stay a short file; a large
    # one is itself a sign the mode never got set.
    logger.add(
        "signal_engine/logs/signal_engine_unknown_{time:YYYY-MM-DD}.log",
        filter=lambda record: _current_mode not in ("live", "analyze"),
        **_file_kwargs,
    )

    # Errors only, one JSON object per line - kept as ONE combined stream across modes
    # deliberately, unlike the file sinks above: after a bad session the first question is
    # "what broke", full stop, not "what broke in which mode" - splitting this one would cost
    # the one-glance view of everything that went wrong today for a distinction that matters
    # far less for errors than for routine trade activity. Mirrors the house convention in the
    # root CLAUDE.md, where log/errors.jsonl is the documented first place to look.
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
