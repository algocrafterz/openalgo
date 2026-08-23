"""Generic signal parser — converts raw Telegram text to Signal objects."""

import re
from typing import Optional

from loguru import logger

from signal_engine.models import Direction, Signal

_KV_PATTERN = re.compile(r"^(\w+)\s*:\s*(.+)$", re.IGNORECASE)
_VALID_DIRECTIONS = {d.value for d in Direction}
_MANDATORY_FIELDS = {"symbol", "entry", "sl", "tp"}
_NUMERIC_FIELDS = {"entry", "sl", "tp"}


def parse(text: str) -> Optional[Signal]:
    """Parse a signal message into a Signal object.

    Standardized format:
        STRATEGY DIRECTION
        Symbol: RELIANCE
        Entry: 2500.50
        SL: 2480
        TP: 2540
        Exchange: NSE      (optional, default from config)
        Product: MIS       (optional, default from config)
        Time: 09:20        (optional)
    """
    if not text or not text.strip():
        return None

    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None

    header = _parse_header(lines[0])
    if header is None:
        return None
    strategy, direction_str = header

    fields = _parse_fields(lines[1:])
    if fields is None:
        return None

    try:
        return Signal(
            strategy=strategy,
            direction=Direction(direction_str),
            symbol=fields["symbol"].upper(),
            entry=fields["entry"],
            sl=fields["sl"],
            tp=fields["tp"],
            exchange=_upper_or_none(fields.get("exchange")),
            product=_upper_or_none(fields.get("product")),
            time=fields.get("time"),
            # TP level from TP HIT normalizer (e.g. "TP1", "TP1.5")
            tp_level=_upper_or_none(fields.get("tplevel")),
            exit_qty_pct=_parse_exit_qty_pct(fields.get("exitqtypct")),
            raw_message=text,
        )
    except Exception as e:
        logger.debug(f"Failed to create Signal: {e}")
        return None


def _parse_header(first_line: str) -> Optional[tuple]:
    """Read "STRATEGY DIRECTION" off the first line. None if it is not one."""
    parts = first_line.strip().split()
    if len(parts) < 2:
        return None
    direction_str = parts[1].upper()
    if direction_str not in _VALID_DIRECTIONS:
        return None
    return parts[0].upper(), direction_str


def _parse_fields(body_lines) -> Optional[dict]:
    """Read the Key: Value body. None if a mandatory or numeric field is unusable."""
    fields = {}
    for line in body_lines:
        match = _KV_PATTERN.match(line.strip())
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()

    if not _MANDATORY_FIELDS.issubset(fields.keys()):
        return None

    for field in _NUMERIC_FIELDS:
        try:
            fields[field] = float(fields[field])
        except (ValueError, TypeError):
            return None
    return fields


def _upper_or_none(value):
    """Upper-case an optional string field, leaving None/empty untouched."""
    return value.upper() if value else value


def _parse_exit_qty_pct(raw) -> Optional[float]:
    """Convert PineScript's ExitQtyPct (0-100) to a 0.0-1.0 fraction.

    None means full exit (backward compatible with alerts that omit the field).
    """
    if raw is None:
        return None
    try:
        return max(0.0, min(1.0, float(raw) / 100.0))
    except (ValueError, TypeError):
        return None
