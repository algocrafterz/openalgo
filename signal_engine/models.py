"""Pydantic v2 data models for the signal engine pipeline."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


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


class PriceType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class BracketLeg(str, Enum):
    ENTRY = "ENTRY"
    SL = "SL"
    TP = "TP"


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


class ValidationResult(BaseModel):
    status: ValidationStatus
    reason: str = ""
