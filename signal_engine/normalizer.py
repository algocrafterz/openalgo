"""Normalizer — preprocesses noisy signal messages into canonical parser format.

Canonical format:
    STRATEGY DIRECTION
    Symbol: SYMBOL
    Entry: 123.45
    SL: 120.00
    TP: 130.00
    Exchange: NSE      (optional)
    Product: MIS       (optional)
    Time: 09:20        (optional)

The normalizer handles:
- Emoji/unicode decoration stripping
- Separator line removal (dashes, equals, underscores)
- TP HIT alerts: "[STRATEGY] TP1 HIT | SYMBOL" -> canonical "STRATEGY EXIT" format (0.0 placeholders)
- Pipe-delimited first line: "STRATEGY LONG | SYMBOL" or "STRATEGY EXIT | SYMBOL" -> two lines
- Legacy "Target:" -> "TP:" key aliasing
- Whitespace cleanup
"""

import re
from typing import Optional

from signal_engine.strategies import ORB as _DEFAULT_STRATEGY

# Emoji and decorative unicode ranges
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # misc symbols, emoticons, dingbats
    "\U00002600-\U000027BF"  # misc symbols
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero-width joiner
    "\U00002702-\U000027B0"  # dingbats
    "\U00002B50-\U00002B55"  # stars
    "\U000023E9-\U000023FA"  # media controls
    "\U00002705\U00002714\U00002716\U0000274C\U0000274E"  # check/cross marks
    "]+",
    flags=re.UNICODE,
)

# Separator-only lines: dashes, equals, underscores, box-drawing chars
_SEPARATOR_RE = re.compile(r"^[\-=_\s\u2500-\u257F]+$")

# Legacy alias: "Target:" -> "TP:"
_TARGET_ALIAS_RE = re.compile(r"^Target\s*:\s*(.+)$", re.IGNORECASE)

# Pipe-delimited first line: STRATEGY DIRECTION | SYMBOL
_PIPE_FIRST_LINE_RE = re.compile(
    r"^([\w-]+\s+(?:LONG|SHORT|EXIT))\s*\|\s*(\w+)$",
    re.IGNORECASE,
)

# TP HIT alert: optional strategy prefix + TP level + HIT + symbol
# Formats: "TP1 HIT | SYMBOL", "ORB TP1 HIT | SYMBOL", "RSI-TP-MR TP1 HIT | SYMBOL"
# Group 1: strategy prefix (optional, [\w-]+ to support hyphens)
# Group 2: TP level (e.g. "TP1", "TP1.5", "TP2")
# Group 3: symbol
# Bare format (no prefix) defaults to _DEFAULT_STRATEGY for backward compatibility.
_TP_HIT_RE = re.compile(
    r"^(?:([\w-]+)\s+)?(TP\d+(?:\.\d+)?)\s+HIT\s*\|\s*(\w+)$",
    re.IGNORECASE,
)


def normalize(text: Optional[str]) -> str:
    """Preprocess a raw signal message into canonical parser format.

    Args:
        text: Raw message text (may contain emojis, separators, pipe notation).

    Returns:
        Cleaned text in canonical format, or empty string if input is empty.
    """
    if not text or not text.strip():
        return ""

    # Strip emoji/unicode decorations
    cleaned = _EMOJI_RE.sub("", text)

    # Split into lines, strip whitespace
    lines = [line.strip() for line in cleaned.strip().splitlines()]

    # Remove empty and separator-only lines
    lines = [line for line in lines if line and not _SEPARATOR_RE.match(line)]

    if not lines:
        return ""

    # Handle TP HIT alert: "[STRATEGY] TP1 HIT | SYMBOL" -> canonical EXIT format
    # Group 1: optional strategy prefix; Group 2: symbol
    # Bare format (no prefix) defaults to _DEFAULT_STRATEGY for backward compatibility.
    # Entry/SL/TP synthesized as 0.0 (validator skips them for EXIT direction).
    # ExitQtyPct field (if present in alert body) is passed through — signal engine
    # uses it directly so no tp_levels config is needed in config.yaml.
    tp_hit_match = _TP_HIT_RE.match(lines[0])
    if tp_hit_match:
        strategy = (tp_hit_match.group(1) or _DEFAULT_STRATEGY).upper()
        tp_level = tp_hit_match.group(2).upper()
        symbol = tp_hit_match.group(3).upper()
        # Extract ExitQtyPct from remaining lines if present
        exit_qty_pct_line = ""
        for line in lines[1:]:
            if re.match(r"^ExitQtyPct\s*:\s*\d+", line, re.IGNORECASE):
                exit_qty_pct_line = f"\n{line}"
                break
        return (
            f"{strategy} EXIT\nSymbol: {symbol}\n"
            f"Entry: 0.0\nSL: 0.0\nTP: 0.0\nTpLevel: {tp_level}{exit_qty_pct_line}"
        )

    # Handle pipe-delimited first line: "ORB LONG | NATIONALUM"
    pipe_match = _PIPE_FIRST_LINE_RE.match(lines[0])
    if pipe_match:
        strategy_direction = pipe_match.group(1).strip()
        symbol = pipe_match.group(2).strip().upper()
        lines = [strategy_direction, f"Symbol: {symbol}"] + lines[1:]

    # Alias legacy "Target:" -> "TP:"
    lines = [_TARGET_ALIAS_RE.sub(r"TP: \1", line) for line in lines]

    return "\n".join(lines)
