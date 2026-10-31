"""Reading the platform's logs for one IST day: shared by preflight, liveness and the log scan."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SE_LOGS = os.path.join(REPO, "signal_engine", "logs")
APP_ERRORS = os.path.join(REPO, "log", "errors.jsonl")

#: Numbers, order ids, prices and hex ids vary per occurrence; the signature is the rest.
_VARIABLE = re.compile(r"0x[0-9a-f]+|\d+(?:[.:]\d+)*")


def signature(logger_name: str, message: str, symbol: str = "") -> str:
    """One line per kind of error: the logger plus the message with its variable parts masked.

    The record's symbol is masked too, or a timeout on KAYNES and one on JSWENERGY read as two
    different errors.
    """
    first = (message or "").strip().splitlines()[0] if message else ""
    if symbol and symbol != "-":
        first = re.sub(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", "<SYM>", first)
    return f"{logger_name}: {_VARIABLE.sub('#', first)[:140]}"


def engine_log(day: str) -> str:
    return os.path.join(SE_LOGS, f"signal_engine_analyze_{day}.log")


def engine_lines(day: str, start: str = "00:00", end: str = "23:59") -> list[str]:
    """Lines of the day's engine log whose HH:MM is within [start, end]."""
    path = engine_log(day)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as handle:
        return [line for line in handle if line[:10] == day and start <= line[11:16] <= end]


def _records(path: str) -> Iterator[dict]:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def engine_errors(day: str) -> Iterator[tuple[str, str, str, str]]:
    """(HH:MM:SS, logger, message, symbol) for each record in signal_engine/logs/errors_<day>.jsonl.

    One JSON object per line: time (ISO, +05:30), level, logger, module, message, ...
    """
    for record in _records(os.path.join(SE_LOGS, f"errors_{day}.jsonl")):
        stamp = str(record.get("time", ""))
        if stamp[:10] == day:
            yield (
                stamp[11:19],
                str(record.get("logger", "signal_engine")),
                str(record.get("message", "")),
                str(record.get("symbol", "")),
            )


def app_errors(day: str) -> Iterator[tuple[str, str, str, str]]:
    """(HH:MM:SS, logger, message, "") for the day's records in OpenAlgo's log/errors.jsonl.

    One JSON object per line: ts ("YYYY-MM-DD HH:MM:SS", IST), level, logger, module, file, message.
    """
    for record in _records(APP_ERRORS):
        stamp = str(record.get("ts", ""))
        if stamp[:10] == day:
            yield (
                stamp[11:19],
                str(record.get("logger", "openalgo")),
                str(record.get("message", "")),
                "",
            )
