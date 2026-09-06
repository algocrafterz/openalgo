"""Pydantic v2 data models for the signal engine pipeline."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    #: Average price the broker actually filled at, when it could be read back.
    #: None means the fill price was never confirmed - NOT that the order was unfilled.
    #: Persisted by db.save so post-trade analysis can measure slippage against the
    #: signal price without depending on a broker tradebook, which is wiped daily and
    #: cannot be re-queried for a past session.
    fill_price: float | None = None


class ValidationResult(BaseModel):
    status: ValidationStatus
    reason: str = ""
