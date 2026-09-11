"""Loguru logging configuration with console and file sinks."""

import json
import os
import sys
import traceback
from datetime import date, timedelta

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

    _file_kwargs = {
        "level": file_level,
        "format": _LOG_FORMAT,
        "rotation": "1 day",
        "retention": "30 days",
        # Frames that produced the error, not just the raising line. `diagnose` stays OFF on
        # purpose: it renders local variable VALUES into the log, and the locals around an
        # order call hold the API key and the broker session token.
        "backtrace": True,
        "diagnose": False,
    }
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
        _error_jsonl_sink,
        level="ERROR",
        backtrace=True,
        diagnose=False,
    )
    return logger


#: loguru's serialize=True renders the whole record, including level.icon — which puts a
#: literal emoji into every line of errors_*.jsonl, against the project's no-icons rule and
#: for no benefit (level.name says the same thing). A small sink lets the icon be dropped
#: while keeping the same one-object-per-line shape everything downstream already reads.
_ERROR_LOG_DIR = "signal_engine/logs"
_ERROR_LOG_RETENTION_DAYS = 90


def _error_record(record) -> dict:
    """The serialisable subset of a loguru record: everything the debugging workflow in the
    root CLAUDE.md asks for, minus the icon."""
    exception = record.get("exception")
    return {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "module": record["module"],
        "function": record["function"],
        "file": f"{record['file'].name}:{record['line']}",
        "message": record["message"],
        "symbol": record["extra"].get("symbol", "-"),
        "process": record["process"].id,
        "thread": record["thread"].id,
        "exception": "".join(
            traceback.format_exception(exception.type, exception.value, exception.traceback)
        ) if exception else None,
    }


def _prune_error_logs() -> None:
    """Delete errors_*.jsonl older than the retention window.

    loguru's own `retention` is not available on a function sink, so this stands in for it —
    called once per file rotation (a new date), which is as often as it needs to run.
    """
    cutoff = date.today() - timedelta(days=_ERROR_LOG_RETENTION_DAYS)
    for name in os.listdir(_ERROR_LOG_DIR):
        if not (name.startswith("errors_") and name.endswith(".jsonl")):
            continue
        try:
            stamp = date.fromisoformat(name[len("errors_"):-len(".jsonl")])
        except ValueError:
            continue
        if stamp < cutoff:
            try:
                os.remove(os.path.join(_ERROR_LOG_DIR, name))
            except OSError:
                pass


_last_error_log_date: "date | None" = None


def _error_jsonl_sink(message) -> None:
    """Append one JSON object per error to errors_<date>.jsonl. Never raises: a logging
    failure must not become the failure."""
    global _last_error_log_date
    try:
        record = message.record
        today = record["time"].date()
        os.makedirs(_ERROR_LOG_DIR, exist_ok=True)
        if _last_error_log_date != today:
            _last_error_log_date = today
            _prune_error_logs()
        path = os.path.join(_ERROR_LOG_DIR, f"errors_{today.isoformat()}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_error_record(record), default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass
