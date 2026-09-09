"""Pydantic v2 data models for the signal engine pipeline."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from signal_engine.timeutils import IST


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    IGNORED = "IGNORED"


class OrderStatus(str, Enum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class Signal(BaseModel):
    strategy: str
    direction: Direction
    symbol: str
    entry: float
    sl: float
    tp: float
    exchange: Optional[str] = None
    product: Optional[str] = None
    time: Optional[str] = None
    # Stable per-trade key emitted by the PineScript on the entry alert and repeated on every
    # TP/SL/EXIT alert for the same trade. Exists BEFORE any order is sent, so it threads a
    # signal that was rejected or never filled — which order_id, the ledger's other bridge,
    # cannot. None on any alert predating the field.
    sig_id: Optional[str] = None
    tp_level: Optional[str] = None          # e.g. "TP1", "TP1.5" — set by TP HIT normalizer
    exit_qty_pct: Optional[float] = None    # 0.0-1.0 fraction of position to exit; from PineScript ExitQtyPct field
    # Every "Key: value" line the pipeline does not consume, lower-cased key -> raw string.
    # Carries the PineScript's entry criteria (Score/RVOL/VF/Auction/AdrUsed/...) through to
    # the trade log untouched, so adding a field to the alert needs no change here.
    context: dict = Field(default_factory=dict)
    raw_message: str = ""
    # Naive IST (tzinfo stripped after computing the IST wall-clock time), matching the
    # codebase-wide "naive datetime means IST" convention - not UTC, and deliberately not
    # timezone-AWARE IST either. isoformat() on an aware datetime appends "+05:30", and
    # SQLite's date()/datetime() functions treat any offset suffix as "convert to UTC first" -
    # confirmed: date('2026-09-10T00:31:47+05:30') returns '2026-09-09'. db.py's
    # `date(executed_at) = ?` queries compare against a plain IST "YYYY-MM-DD" string, so a
    # stored value carrying +05:30 silently gets shifted a day for anything logged before
    # 05:30 IST. See TradeResult.timestamp's comment for the concrete bug this caused.
    received_at: datetime = Field(default_factory=lambda: datetime.now(IST).replace(tzinfo=None))


class Order(BaseModel):
    symbol: str
    exchange: str
    action: Action
    quantity: int
    price: float
    order_type: str
    product: str
    strategy_tag: str
    trigger_price: float = 0.0


class TradeResult(BaseModel):
    order_id: str = ""
    status: OrderStatus
    message: str = ""
    # 2026-09-10: was UTC (datetime.now(timezone.utc)), then briefly timezone-AWARE IST - both
    # wrong for the same underlying reason. db.save() stamps trades.db's executed_at straight
    # from this field, and both db.fetch_all_open_positions() (startup reconciliation) and
    # fetch_last_entry_trade() (engine-restart recovery) filter it with
    # date(executed_at) = <today in IST, a plain "YYYY-MM-DD" string>. SQLite's date() function
    # treats any timezone-offset suffix in its argument as "convert to UTC first, then take the
    # date" - so EITHER a raw UTC timestamp OR an offset-aware "+05:30" IST timestamp gets
    # shifted a day for anything logged before 05:30 IST (confirmed:
    # date('2026-09-10T00:31:47+05:30') returns '2026-09-09'). Only a NAIVE IST timestamp
    # (tzinfo stripped) round-trips correctly through SQLite's date() - matching this codebase's
    # existing "naive datetime means IST" convention everywhere else. Found via a test that only
    # failed once the wall clock crossed midnight IST; during actual market hours
    # (09:15-15:30 IST) this was invisible either way, since a restart during trading hours
    # never lands in the ~5.5 hour post-midnight window where the day genuinely differs.
    timestamp: datetime = Field(default_factory=lambda: datetime.now(IST).replace(tzinfo=None))
    #: Average price the broker actually filled at, when it could be read back.
    #: None means the fill price was never confirmed - NOT that the order was unfilled.
    #: Persisted by db.save so post-trade analysis can measure slippage against the
    #: signal price without depending on a broker tradebook, which is wiped daily and
    #: cannot be re-queried for a past session.
    fill_price: float | None = None


class ValidationResult(BaseModel):
    status: ValidationStatus
    reason: str = ""
