"""Broker-specific fees: the part of a trade's cost that depends on who placed the order.

Statutory charges (STT, exchange transaction fee, SEBI, stamp duty, GST) are the same at every
broker and live in `portfolio.costs`. Brokerage, and DP charges on delivery sells, are the
broker's own, so they are kept here, one entry per broker.

To add a broker: read its published charges page, add a `BrokerFees` below with the page URL
in `source` and the date read in `verified_on`, and set `assumed=False`. Until then a broker
falls back to `DEFAULT_FEES`, flagged `assumed=True` so every figure built on it says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from utils.logging import get_logger

logger = get_logger(__name__)

#: A published schedule can change; ask for a re-check after this many days.
REVERIFY_AFTER_DAYS = 180
_warned: set[str] = set()


@dataclass(frozen=True)
class BrokerFees:
    key: str
    label: str
    #: Intraday (MIS) brokerage as a fraction of each order's value, with a per-order ceiling
    #: in rupees (0 = no ceiling). Rate 0 means no brokerage.
    intraday_rate: float
    intraday_cap: float
    #: Flat rupees per delivery (CNC) order.
    delivery_flat: float
    #: Rupees per scrip debit on a delivery sell, before GST.
    dp_charge: float
    #: True when these numbers are a stand-in rather than read from the broker's own page.
    assumed: bool
    source: str = ""
    verified_on: str = ""


#: A typical discount broker: 0.03% or Rs 20 per intraday order, whichever is lower, free
#: delivery. Used for any broker not listed below, and flagged assumed.
DEFAULT_FEES = BrokerFees(
    key="default",
    label="Generic discount broker (assumed)",
    intraday_rate=0.0003,
    intraday_cap=20.0,
    delivery_flat=0.0,
    dp_charge=0.0,
    assumed=True,
)

BROKER_FEES: dict[str, BrokerFees] = {
    "flattrade": BrokerFees(
        key="flattrade",
        label="Flattrade",
        intraday_rate=0.0,
        intraday_cap=0.0,
        delivery_flat=0.0,
        dp_charge=20.0,
        assumed=False,
        source="https://flattrade.in/charges/",
        verified_on="2026-10-01",
    ),
}


def fees_for(broker: str | None) -> BrokerFees:
    """The fee schedule for `broker`, or the assumed default when it is blank or not modelled.

    A named broker with no entry is logged once, loudly: its charges are an estimate until
    someone reads its published page and adds it.
    """
    key = (broker or "").strip().lower()
    found = BROKER_FEES.get(key)
    if found is not None:
        return found
    if key and key != "unknown" and key not in _warned:
        _warned.add(key)
        logger.warning(
            f"No published fee schedule for broker '{key}': its trades are priced on the assumed "
            "default. Read its charges page and add an entry to portfolio/broker_fees.py."
        )
    return DEFAULT_FEES


def attention_for(broker: str | None, today: date | None = None) -> str | None:
    """Why this broker's charges deserve a look, or None when they are verified and current."""
    fees = fees_for(broker)
    key = (broker or "").strip().lower()
    if fees.assumed:
        if not key or key == "unknown":
            return "broker not recorded for some trades: priced on an assumed discount-broker rate"
        return (
            f"no published fee schedule for '{key}': priced on an assumed discount-broker rate; "
            "add it to portfolio/broker_fees.py"
        )
    if fees.verified_on:
        age = ((today or date.today()) - date.fromisoformat(fees.verified_on)).days
        if age > REVERIFY_AFTER_DAYS:
            return (
                f"{fees.label} fees were last read {age} days ago ({fees.verified_on}): "
                f"re-verify against {fees.source}"
            )
    return None


def uncovered_brokers(plugin_brokers: list[str]) -> list[str]:
    """Which of the brokers OpenAlgo can run have no verified fee schedule yet."""
    return [b for b in plugin_brokers if b.strip().lower() not in BROKER_FEES]
