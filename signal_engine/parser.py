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

    # First line: STRATEGY DIRECTION
    first_line_parts = lines[0].strip().split()
    if len(first_line_parts) < 2:
        return None

    strategy = first_line_parts[0].upper()
    direction_str = first_line_parts[1].upper()

    if direction_str not in _VALID_DIRECTIONS:
        return None

    # Remaining lines: key-value pairs
    fields = {}
    for line in lines[1:]:
        match = _KV_PATTERN.match(line.strip())
        if match:
            key = match.group(1).lower()
            value = match.group(2).strip()
            fields[key] = value

    # Check mandatory fields
    if not _MANDATORY_FIELDS.issubset(fields.keys()):
        return None

    # Convert numeric fields
    for field in _NUMERIC_FIELDS:
        try:
            fields[field] = float(fields[field])
        except (ValueError, TypeError):
            return None

    # Uppercase optional string fields
    exchange = fields.get("exchange")
    if exchange:
        exchange = exchange.upper()
    product = fields.get("product")
    if product:
        product = product.upper()

    try:
        return Signal(
            strategy=strategy,
            direction=Direction(direction_str),
            symbol=fields["symbol"].upper(),
            entry=fields["entry"],
            sl=fields["sl"],
            tp=fields["tp"],
            exchange=exchange,
            product=product,
            time=fields.get("time"),
            raw_message=text,
        )
    except Exception as e:
        logger.debug(f"Failed to create Signal: {e}")
        return None
