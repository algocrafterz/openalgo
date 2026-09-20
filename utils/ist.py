"""Shared IST (India Standard Time) offset.

OpenAlgo trades only Indian NSE/BSE markets, so every broker adapter and the
signal engine convert timestamps to IST. This offset was previously
redefined independently across ~15 modules; define it once here.
"""

from datetime import timedelta, timezone

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)
