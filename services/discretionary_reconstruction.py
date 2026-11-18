# services/discretionary_reconstruction.py
"""
Pure position/trade reconstruction for the Discretionary Trading Performance module.

Stage 2 engine: fills in -> positions, closed trades, fill links and orphans out. No I/O, no
clock, no database, no broker, no charges (see services/discretionary_charges.py).

The accounting is the weighted-average method of ``database/strategy_book_db`` apply_fill:

* A fill in the position's direction (or into a flat position) raises the weighted average
  entry price. ``opened_at`` is set only when the position goes from flat to open.
* A fill against the position closes ``min(fill, position)`` at the current average entry as
  ONE closed trade (so every partial exit makes its own trade row). Any remainder opens the
  opposite position at the fill price, with a new ``opened_at`` and a new ``position_seq``.

Differences from apply_fill, on purpose: values are never rounded here (the strategy book
rounds quantity/price/pnl to 4 places; rounding would break the P&L identity checked by the
tests), and a fill that would reduce a position that has no recorded opening is not turned
into a position: it goes to the orphan list (``opening_unknown``). A fill may open or extend
a SHORT only on a derivatives exchange (NFO, BFO, MCX, CDS, BCD, NCO) whatever the product, or
for product MIS on any exchange (see ``can_open_short``). CNC never opens a short; NRML (or any
other product) on NSE/BSE cash never does either: a sell there with no recorded long means
holdings bought before the ledger, so it is an orphan flagged ``opening_unknown``.

Position identity is (broker, account, exchange, symbol, product); different accounts and
different products never net or merge.

Output links tie every used unit of a fill to a leg, so charges can be allocated per order:

  {"fill_ref", "trade_idx" | "pos_idx", "role": "ENTRY" | "EXIT", "qty_used"}

An ENTRY link with ``trade_idx`` is the part of an entry fill that a closed trade consumed
(weighted-average: each entry fill is consumed pro rata); an ENTRY link with ``pos_idx`` is
what is still open. The EXIT link is the closing part of the closing fill. Summed over all its
links, a fill's ``qty_used`` equals its quantity, except for an orphaned excess.
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta

# "2": derivatives (and MIS) can open shorts whatever the product (was MIS only).
ENGINE_VERSION = "2"
EPS = 1e-9

DERIVATIVE_EXCHANGES = frozenset({"NFO", "BFO", "MCX", "CDS", "BCD", "NCO", "NCDEX"})

ROLE_ENTRY = "ENTRY"
ROLE_EXIT = "EXIT"

REASON_INVALID = "invalid_fill"
REASON_OPENING_UNKNOWN = "opening_unknown"
REASON_EXCESS_OPENING_UNKNOWN = "excess_over_position_opening_unknown"


def identity_of(fill: dict) -> tuple:
    return (
        fill["broker"],
        fill["account"],
        fill["exchange"],
        fill["symbol"],
        fill["product"],
    )


def session_expiry() -> tuple[int, int]:
    """(hour, minute) of SESSION_EXPIRY_TIME (default 03:00), as utils/session.py reads it."""
    raw = os.getenv("SESSION_EXPIRY_TIME", "03:00")
    try:
        hour, minute = map(int, raw.split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return 3, 0


def session_date(moment: datetime, expiry: tuple[int, int] | None = None) -> date:
    """Trading-session date of a naive IST time: the previous day before the expiry time."""
    hour, minute = expiry or session_expiry()
    boundary = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment < boundary:
        return (moment - timedelta(days=1)).date()
    return moment.date()


def can_open_short(exchange, product) -> bool:
    """True when a sell may open (or extend) a short: derivatives segment, or product MIS."""
    return (
        str(exchange or "").upper() in DERIVATIVE_EXCHANGES or str(product or "").upper() == "MIS"
    )


def sort_key(fill: dict) -> tuple:
    """Deterministic processing order: (fill_time, order_id, fill_id or '', id)."""
    return (
        fill["fill_time"],
        str(fill.get("order_id") or ""),
        str(fill.get("fill_id") or ""),
        fill["id"],
    )


def _valid(fill: dict) -> bool:
    try:
        qty, price = float(fill["qty"]), float(fill["price"])
    except (KeyError, TypeError, ValueError):
        return False
    if not (math.isfinite(qty) and math.isfinite(price)) or qty <= 0 or price <= 0:
        return False
    if str(fill.get("side", "")).upper() not in ("BUY", "SELL"):
        return False
    return isinstance(fill.get("fill_time"), datetime)


def hold_minutes(opened: datetime, closed: datetime) -> float | None:
    """Same rule as strategy_trade_enrichment.hold_minutes: 2 places, None when negative."""
    minutes = (closed - opened).total_seconds() / 60.0
    return round(minutes, 2) if minutes >= 0 else None


class _Leg:
    """Mutable state of one identity while its fills are replayed."""

    def __init__(self) -> None:
        self.qty = 0.0  # signed: > 0 long, < 0 short
        self.avg = 0.0
        self.seq = 0
        self.pos: dict | None = None
        self.entries: list[list] = []  # [fill_ref, remaining qty] of the open position
        self.unknown = False  # an orphan happened on this identity


def reconstruct(
    fills: list[dict],
    *,
    strict_opening: bool = True,
    expiry: tuple[int, int] | None = None,
) -> dict:
    """Replay ``fills`` and return {"positions", "trades", "links", "orphans"}.

    ``fills`` need id, broker, account, order_id, fill_id, symbol, exchange, product, side,
    qty, price and fill_time (naive IST). The input is not modified and its order does not
    matter: the engine sorts by (fill_time, order_id, fill_id or '', id).

    ``strict_opening=False`` lets every product on every exchange open a short (used only to
    net ALL ledger fills for the PositionBook comparison, never for discretionary P&L).
    """
    expiry = expiry or session_expiry()
    positions: list[dict] = []
    trades: list[dict] = []
    links: list[dict] = []
    orphans: list[dict] = []
    legs: dict[tuple, _Leg] = {}

    for fill in sorted(fills, key=sort_key):
        if not _valid(fill):
            orphans.append(_orphan(fill, REASON_INVALID, _qty_or_zero(fill)))
            continue
        ident = identity_of(fill)
        leg = legs.setdefault(ident, _Leg())
        qty = float(fill["qty"])
        price = float(fill["price"])
        is_buy = str(fill["side"]).upper() == "BUY"
        signed = qty if is_buy else -qty
        can_short = (not strict_opening) or can_open_short(fill["exchange"], fill["product"])

        if abs(leg.qty) < EPS:
            if not is_buy and not can_short:
                leg.unknown = True
                orphans.append(_orphan(fill, REASON_OPENING_UNKNOWN, qty))
                continue
            _open(positions, leg, ident, fill, signed, price, expiry)
            continue

        if (leg.qty > 0) == (signed > 0):
            total = abs(leg.qty) + abs(signed)
            leg.avg = (leg.avg * abs(leg.qty) + price * abs(signed)) / total
            leg.qty += signed
            leg.entries.append([fill["id"], qty])
            _sync(leg)
            continue

        # Opposite direction: close min(fill, position), the remainder reopens.
        closing = min(qty, abs(leg.qty))
        remaining = qty - closing
        direction = 1.0 if leg.qty > 0 else -1.0
        pos = leg.pos
        assert pos is not None
        trade_idx = len(trades)
        trades.append(
            {
                "broker": ident[0],
                "account": ident[1],
                "exchange": ident[2],
                "symbol": ident[3],
                "product": ident[4],
                "position_seq": pos["position_seq"],
                "direction": "LONG" if direction > 0 else "SHORT",
                "closed_quantity": closing,
                "entry_price": leg.avg,
                "exit_price": price,
                "opened_at": pos["opened_at"],
                "closed_at": fill["fill_time"],
                "trade_date": session_date(fill["fill_time"], expiry).isoformat(),
                "overnight": session_date(pos["opened_at"], expiry)
                != session_date(fill["fill_time"], expiry),
                "hold_minutes": hold_minutes(pos["opened_at"], fill["fill_time"]),
                "realized_pnl": closing * (price - leg.avg) * direction,
                "closing_fill": fill["id"],
            }
        )
        pos["realized_gross"] += trades[-1]["realized_pnl"]
        factor = closing / abs(leg.qty)
        for entry in leg.entries:
            used = entry[1] * factor
            entry[1] -= used
            if used > EPS:
                links.append(
                    {
                        "fill_ref": entry[0],
                        "trade_idx": trade_idx,
                        "role": ROLE_ENTRY,
                        "qty_used": used,
                    }
                )
        links.append(
            {"fill_ref": fill["id"], "trade_idx": trade_idx, "role": ROLE_EXIT, "qty_used": closing}
        )
        leg.qty += signed * (closing / qty)  # move toward zero by the closed part only
        if abs(leg.qty) < EPS:
            leg.qty = 0.0
            leg.avg = 0.0
            leg.entries = []
            pos["status"] = "FLAT"
            pos["open_qty"] = 0.0
            pos["avg_entry"] = 0.0
            leg.pos = None
        else:
            _sync(leg)
        if remaining > EPS:
            rem_signed = remaining if is_buy else -remaining
            if not is_buy and not can_short:
                leg.unknown = True
                orphans.append(_orphan(fill, REASON_EXCESS_OPENING_UNKNOWN, remaining))
            else:
                _open(positions, leg, ident, fill, rem_signed, price, expiry)

    _finish_positions(positions, links, legs)
    return {"positions": positions, "trades": trades, "links": links, "orphans": orphans}


def _qty_or_zero(fill: dict) -> float:
    try:
        return float(fill.get("qty") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _orphan(fill: dict, reason: str, qty: float) -> dict:
    return {
        "fill_ref": fill.get("id"),
        "reason": reason,
        "qty": qty,
        "broker": fill.get("broker"),
        "account": fill.get("account"),
        "exchange": fill.get("exchange"),
        "symbol": fill.get("symbol"),
        "product": fill.get("product"),
        "side": fill.get("side"),
        "price": fill.get("price"),
        "fill_time": fill.get("fill_time"),
    }


def _open(positions, leg: _Leg, ident, fill: dict, signed: float, price: float, expiry) -> None:
    """Start a new position from flat with ``signed`` quantity at ``price``."""
    leg.seq += 1
    leg.qty = signed
    leg.avg = price
    leg.entries = [[fill["id"], abs(signed)]]
    pos = {
        "broker": ident[0],
        "account": ident[1],
        "exchange": ident[2],
        "symbol": ident[3],
        "product": ident[4],
        "position_seq": leg.seq,
        "direction": "LONG" if signed > 0 else "SHORT",
        "open_qty": abs(signed),
        "avg_entry": price,
        "opened_at": fill["fill_time"],
        "opened_session": session_date(fill["fill_time"], expiry).isoformat(),
        "realized_gross": 0.0,
        "entry_charges_carried": None,
        "opening_unknown": leg.unknown,
        "status": "OPEN",
    }
    leg.pos = pos
    positions.append(pos)


def _sync(leg: _Leg) -> None:
    if leg.pos is not None:
        leg.pos["open_qty"] = abs(leg.qty)
        leg.pos["avg_entry"] = leg.avg


def _finish_positions(positions: list[dict], links: list[dict], legs: dict) -> None:
    """Add ENTRY links for what is still open, and flag identities that had an orphan."""
    index = {
        (p["broker"], p["account"], p["exchange"], p["symbol"], p["product"], p["position_seq"]): i
        for i, p in enumerate(positions)
    }
    for ident, leg in legs.items():
        if leg.pos is None:
            continue
        pos_idx = index[(*ident, leg.pos["position_seq"])]
        for fill_ref, remaining in leg.entries:
            if remaining > EPS:
                links.append(
                    {
                        "fill_ref": fill_ref,
                        "pos_idx": pos_idx,
                        "role": ROLE_ENTRY,
                        "qty_used": remaining,
                    }
                )
        if leg.unknown:
            leg.pos["opening_unknown"] = True
