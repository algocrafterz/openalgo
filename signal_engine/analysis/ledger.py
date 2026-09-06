"""Reconcile signal -> order -> broker fill into one round-trip position ledger.

WHY THIS EXISTS

A Telegram channel cannot measure trade quality, for three reasons that are structural
rather than fixable by better parsing:

  1. IT DOES NOT KNOW WHAT WAS FILLED. The alert advertises a price computed on a closed
     bar. The engine sends a MARKET order some seconds later and the broker fills it
     somewhere else. That difference is slippage, it is the single largest controllable
     cost in the system, and it is invisible in the channel by construction.
  2. IT DOES NOT SEE PARTIAL EXITS PROPERLY. TP1 books 50%; the rest leaves at TP1.5, on
     the engine's own 14:45 clock, or via the broker's square-off. Only the first of those
     has an alert. Weighting a trade by a single exit price is simply wrong arithmetic.
  3. IT DOES NOT SEE WHAT NEVER HAPPENED. A signal the validator rejected, an order the
     broker refused for margin, a time exit whose alert failed to parse - all of these
     look identical in the channel to a trade that went fine, because the channel only
     ever shows what was SENT, never what landed.

THREE LAYERS, KEPT SEPARATE

Every leg records all three and never collapses them, because each gap is a different
problem with a different owner:

    signal_price   what the alert advertised          -> strategy's problem
    order_qty      what the engine sized and sent     -> risk engine's problem
    fill_price     what the broker actually did       -> execution's problem

THE JOIN

`order_id` is the only reliable bridge. `trades.db` records it for every order the engine
placed; the broker's tradebook records it against every fill. Symbol-and-time matching is
a fallback that will silently mis-pair two trades in the same name on the same day, so it
is never used for anything the P&L depends on.

WHAT IT DELIBERATELY DOES NOT DO

It does not compute a strategy verdict. Live samples are small and self-selected - you
only ran the strategies you believed in - so treating this ledger as a backtest would be
survivorship analysis. Use it to find LEAKS: slippage, unfilled orders, orphaned
positions, exits that never fired. Those are real at n=20.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from signal_engine.timeutils import IST

#: The same file `db.py` writes. Analysis reads the engine's own audit trail, not a copy.
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "trades.db"
)

#: Reconciliation outcomes. These ARE the report - a clean ledger is not the interesting
#: case, the flagged rows are.
FLAG_NO_FILL = "NO_FILL"  # engine sent it, broker has no trade for it
FLAG_QTY_MISMATCH = "QTY_MISMATCH"  # filled quantity differs from what was ordered
FLAG_UNMATCHED_FILL = "UNMATCHED_FILL"  # broker filled something the engine never sent
FLAG_OPEN_AT_EOD = "OPEN_AT_EOD"  # entry never fully closed by any exit
FLAG_OVER_EXITED = "OVER_EXITED"  # exits total more than the entry quantity
FLAG_NO_ENTRY = "NO_ENTRY"  # exit event with no preceding entry

#: Rows written by db.save_declined() — a signal that never became an order.
_DECLINED_STATUS = "DECLINED"


@dataclass
class Leg:
    """One order in a position's life: the entry, or one exit tranche."""

    kind: str  # "ENTRY" | "EXIT"
    at: datetime | None  # when the engine acted
    signal_price: float  # what the alert advertised
    order_qty: int  # what the engine sent
    order_id: str
    status: str  # OrderStatus from trades.db
    reason: str = ""  # TP1 / SL / TIME_EXIT / ... from the alert context
    fill_price: float | None = None  # from the broker tradebook, None if never filled
    fill_qty: int = 0
    fill_at: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return self.fill_price is not None and self.fill_qty > 0

    def slippage_bps(self, direction: int) -> float | None:
        """Fill vs advertised price, in bps of notional, signed so + means it cost money.

        An entry filled ABOVE the advertised price costs a long money; an exit filled
        BELOW it costs a long money. Both come out positive here so they can be summed.
        """
        if not self.filled or not self.signal_price:
            return None
        adverse = (
            (self.fill_price - self.signal_price)
            if self.kind == "ENTRY"
            else (self.signal_price - self.fill_price)
        )
        return adverse * direction / self.signal_price * 10000.0


@dataclass
class Position:
    """One round trip: an entry and every exit tranche that closed it."""

    strategy: str
    symbol: str
    day: date
    direction: int  # +1 long, -1 short
    entry: Leg
    exits: list[Leg] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    signal_sl: float = 0.0
    signal_tp: float = 0.0
    #: The PineScript's per-trade key, when the alerts carried one. None for rows written
    #: before SigID existed, which reconcile by the arrival-order rule instead.
    sig_id: str | None = None

    # ---- quantities -----------------------------------------------------

    @property
    def entry_qty(self) -> int:
        return self.entry.fill_qty if self.entry.filled else 0

    @property
    def exited_qty(self) -> int:
        return sum(x.fill_qty for x in self.exits if x.filled)

    @property
    def residual_qty(self) -> int:
        return self.entry_qty - self.exited_qty

    # ---- geometry -------------------------------------------------------

    @property
    def risk_per_share(self) -> float:
        """Stop distance from the ADVERTISED entry - the R the strategy intended."""
        return abs(self.entry.signal_price - self.signal_sl) if self.signal_sl else 0.0

    @property
    def avg_exit(self) -> float | None:
        """Quantity-weighted exit. The arithmetic a single-exit view gets wrong."""
        legs = [x for x in self.exits if x.filled]
        q = sum(x.fill_qty for x in legs)
        return sum(x.fill_price * x.fill_qty for x in legs) / q if q else None

    # ---- results --------------------------------------------------------

    @property
    def gross_pnl(self) -> float | None:
        """Rupees, on the quantity that actually round-tripped. None if still open."""
        if not self.entry.filled:
            return None
        legs = [x for x in self.exits if x.filled]
        if not legs:
            return None
        return sum(
            (x.fill_price - self.entry.fill_price) * self.direction * x.fill_qty for x in legs
        )

    @property
    def realised_r(self) -> float | None:
        """P&L in units of the risk the strategy intended to take.

        Denominator is `risk_per_share * quantity that round-tripped` - NOT the full
        entry quantity. A position still holding half is not a full R of exposure, and
        charging it as one understates every partial-exit strategy.
        """
        pnl = self.gross_pnl
        if pnl is None or self.risk_per_share <= 0 or self.exited_qty <= 0:
            return None
        return pnl / (self.risk_per_share * self.exited_qty)

    @property
    def entry_slippage_bps(self) -> float | None:
        return self.entry.slippage_bps(self.direction)

    @property
    def exit_slippage_bps(self) -> float | None:
        """Quantity-weighted exit slippage. SL legs are excluded on purpose.

        An SL-M fill is not slippage against a signal price - the alert reports the stop
        LEVEL while the broker fills wherever the market was when it triggered. Mixing
        that gap into a slippage number makes execution look far worse than it is and
        hides the entry slippage, which is the number you can actually do something about.
        """
        legs = [
            x for x in self.exits if x.filled and "SL" not in x.reason.upper() and x.signal_price
        ]
        q = sum(x.fill_qty for x in legs)
        if not q:
            return None
        return sum(x.slippage_bps(self.direction) * x.fill_qty for x in legs) / q

    @property
    def hold_minutes(self) -> float | None:
        legs = [x for x in self.exits if x.filled and x.at]
        if not legs or not self.entry.at:
            return None
        return (max(x.at for x in legs) - self.entry.at).total_seconds() / 60.0

    @property
    def exit_reasons(self) -> str:
        """Exit path as a string, e.g. "TP1+TIME_EXIT".

        Reports EVERY exit leg the engine sent, filled or not, with an unfilled one marked
        `!`. An exit that was ordered and never filled is the most expensive thing this
        ledger can find - silently dropping it would hide exactly what we are looking for.
        """
        if not self.exits:
            return "NO_EXIT_EVENT"
        return "+".join((x.reason or "?") + ("" if x.filled else "!") for x in self.exits)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def load_engine_events(db_path: str | None = None, since: date | None = None) -> list[dict]:
    """Every order the engine placed, from `trades.db`, oldest first.

    One row per SIGNAL the engine acted on - an entry and each exit are separate rows.
    Rows whose order never succeeded are kept, not filtered: an order the broker rejected
    is exactly the kind of leak this package exists to surface.
    """
    path = db_path or _DB_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"no trades.db at {path} - run the engine, or pass db_path explicitly"
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades ORDER BY COALESCE(executed_at, received_at) ASC"
            )
        ]
    finally:
        conn.close()

    out = []
    for r in rows:
        # Declines never became an order. Letting them through would make every one a
        # Position whose entry leg has no fill, i.e. a NO_FILL flag -- a reconciliation error
        # that is not one, burying the real unfilled orders. load_declined() reads them.
        if (r.get("status") or "").upper() == _DECLINED_STATUS:
            continue
        ts = _parse_ts(r.get("executed_at") or r.get("received_at"))
        if ts is None:
            continue
        if since and ts.date() < since:
            continue
        try:
            r["context"] = json.loads(r.get("context") or "{}")
        except (ValueError, TypeError):
            r["context"] = {}
        r["_ts"] = ts
        out.append(r)
    return out


def load_declined(db_path: str | None = None, since: date | None = None) -> list[dict]:
    """Signals the engine refused before sending an order, newest last.

    Kept out of the position ledger on purpose and read separately: a decline is not a broken
    trade, it is a trade that never started. The pair of numbers -- taken vs declined, with the
    reason -- is what tells a review whether a thin week was the strategy finding nothing or
    the risk limits refusing what it found.
    """
    path = db_path or _DB_PATH
    if not os.path.exists(path):
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trades WHERE UPPER(status) = ? "
                "ORDER BY COALESCE(executed_at, received_at) ASC",
                (_DECLINED_STATUS,),
            )
        ]
    finally:
        conn.close()

    out = []
    for r in rows:
        ts = _parse_ts(r.get("executed_at") or r.get("received_at"))
        if ts is None or (since and ts.date() < since):
            continue
        try:
            r["context"] = json.loads(r.get("context") or "{}")
        except (ValueError, TypeError):
            r["context"] = {}
        r["_ts"] = ts
        out.append(r)
    return out


def load_fills(tradebook: Iterable[dict]) -> dict[str, dict]:
    """Broker fills keyed by order id, aggregated across partial fills.

    `tradebook` is the payload of OpenAlgo's `/api/v1/tradebook` `data` list. It is passed
    in rather than fetched so the ledger can be rebuilt offline from a saved snapshot -
    which matters, because a broker tradebook is wiped daily and cannot be re-queried for
    a past session. Snapshot it every evening; see `__main__.py --snapshot`.

    One order can produce several trades. Quantity sums; price is quantity-weighted.
    """
    agg: dict[str, dict] = {}
    for t in tradebook:
        oid = str(t.get("orderid") or "").strip()
        if not oid:
            continue
        qty = int(float(t.get("quantity") or 0))
        px = float(t.get("average_price") or 0)
        if qty <= 0 or px <= 0:
            continue
        a = agg.setdefault(
            oid,
            {
                "qty": 0,
                "notional": 0.0,
                "timestamp": "",
                "symbol": t.get("symbol", ""),
                "action": t.get("action", ""),
            },
        )
        a["qty"] += qty
        a["notional"] += px * qty
        a["timestamp"] = a["timestamp"] or str(t.get("timestamp") or "")
    for a in agg.values():
        a["price"] = a["notional"] / a["qty"] if a["qty"] else 0.0
    return agg


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def build_ledger(events: list[dict], fills: dict[str, dict] | None = None) -> list[Position]:
    """Stitch engine events plus broker fills into round-trip positions.

    Grouping is by (strategy, symbol, session date). Within a group, an ENTRY opens a
    position and every following EXIT belongs to it until the next ENTRY - the same
    ordering rule the live tracker uses, so the ledger reflects how the engine actually
    reconciled, not an idealised pairing.
    """
    fills = fills or {}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in events:
        # SigID, when the alert carried one, is the position's identity: the PineScript
        # assigns it at the entry bar and repeats it on every exit for that trade. Grouping
        # on it makes reconciliation exact instead of positional, which matters for the case
        # the arrival-order rule cannot see -- two round trips in the same name on the same
        # day, where a delayed exit attaches to the wrong entry and nothing in the data shows
        # it happened. Keyless events keep the original (strategy, symbol, day) grouping, so
        # historical rows reconcile exactly as before.
        sig_id = str(e.get("sig_id") or "").strip()
        strategy = e.get("strategy") or ""
        symbol = e.get("symbol") or ""
        key = (
            (strategy, symbol, e["_ts"].date(), sig_id)
            if sig_id
            else (strategy, symbol, e["_ts"].date())
        )
        groups[key].append(e)

    positions: list[Position] = []
    matched_ids: set[str] = set()

    for group_key, evs in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][1], kv[0][3:])):
        strategy, symbol, day = group_key[0], group_key[1], group_key[2]
        group_sig_id = group_key[3] if len(group_key) > 3 else None
        evs.sort(key=lambda e: e["_ts"])
        cur: Position | None = None
        for e in evs:
            leg = _leg_from_event(e, fills)
            if leg.order_id:
                matched_ids.add(leg.order_id)
            if e.get("direction") in ("LONG", "SHORT"):
                if cur is not None:
                    positions.append(_finalise(cur))
                cur = Position(
                    strategy=strategy,
                    symbol=symbol,
                    day=day,
                    direction=1 if e["direction"] == "LONG" else -1,
                    entry=leg,
                    signal_sl=float(e.get("sl") or 0.0),
                    signal_tp=float(e.get("tp") or 0.0),
                    sig_id=group_sig_id,
                )
            elif cur is not None:
                cur.exits.append(leg)
            else:
                # An exit with nothing open. Usually an entry that was never persisted, or
                # a restart that lost tracker state - either way a real reconciliation gap.
                orphan = Position(
                    strategy=strategy,
                    symbol=symbol,
                    day=day,
                    direction=1,
                    entry=leg,
                    flags=[FLAG_NO_ENTRY],
                    sig_id=group_sig_id,
                )
                positions.append(orphan)
        if cur is not None:
            positions.append(_finalise(cur))

    for oid, f in fills.items():
        if oid not in matched_ids:
            positions.append(_unmatched_position(oid, f))
    return positions


def _leg_from_event(e: dict, fills: dict[str, dict]) -> Leg:
    ctx = e.get("context") or {}
    oid = str(e.get("order_id") or "").strip()
    f = fills.get(oid)
    is_entry = e.get("direction") in ("LONG", "SHORT")
    reason = str(ctx.get("reason") or ctx.get("tplevel") or "").strip()
    if not reason and not is_entry:
        head = (e.get("raw_message") or "").split("\n", 1)[0].upper()
        for k in ("TP1.5", "TP1", "TP2", "TP3", "SL", "TIME"):
            if k in head:
                reason = k
                break
    # Fill price, in order of trust:
    #   1. the broker tradebook, which also gives the filled QUANTITY - the only source
    #      that can prove a partial fill;
    #   2. `fill_price` persisted by db.save at execution time. Quantity is assumed equal
    #      to what was ordered, which is right for a MARKET order on a liquid F&O name and
    #      is flagged QTY_MISMATCH by the tradebook path whenever it is not.
    # (2) exists because a broker tradebook is wiped daily: without a snapshot captured
    # that evening, (1) is unavailable forever, and slippage would be unmeasurable.
    ordered = int(e.get("quantity") or 0)
    saved_fill = e.get("fill_price")
    saved_fill = float(saved_fill) if saved_fill not in (None, "", 0) else None
    if f:
        fill_price, fill_qty, fill_at = f["price"], f["qty"], f["timestamp"]
    elif saved_fill and str(e.get("status") or "") == "SUCCESS":
        fill_price, fill_qty, fill_at = saved_fill, ordered, "engine"
    else:
        fill_price, fill_qty, fill_at = None, 0, ""

    return Leg(
        kind="ENTRY" if is_entry else "EXIT",
        at=e["_ts"],
        signal_price=float(e.get("entry") or 0.0) if is_entry else float(e.get("tp") or 0.0),
        order_qty=ordered,
        order_id=oid,
        status=str(e.get("status") or ""),
        reason=reason,
        fill_price=fill_price,
        fill_qty=fill_qty,
        fill_at=fill_at,
        raw=e,
    )


def _finalise(p: Position) -> Position:
    if p.entry.status == "SUCCESS" and not p.entry.filled:
        p.flags.append(FLAG_NO_FILL)
    if (p.entry.filled and p.entry.order_qty and p.entry.fill_at != "engine"
            and p.entry.fill_qty != p.entry.order_qty):
        p.flags.append(FLAG_QTY_MISMATCH)
    for x in p.exits:
        if x.status == "SUCCESS" and not x.filled:
            p.flags.append(FLAG_NO_FILL)
            break
    if p.entry.filled:
        if p.residual_qty > 0:
            p.flags.append(FLAG_OPEN_AT_EOD)
        elif p.residual_qty < 0:
            p.flags.append(FLAG_OVER_EXITED)
    return p


def _unmatched_position(order_id: str, f: dict) -> Position:
    """A broker fill the engine never asked for: manual trade, or auto square-off."""
    leg = Leg(
        kind="EXIT",
        at=None,
        signal_price=0.0,
        order_qty=0,
        order_id=order_id,
        status="BROKER_ONLY",
        reason="UNMATCHED",
        fill_price=f["price"],
        fill_qty=f["qty"],
        fill_at=f["timestamp"],
    )
    return Position(
        strategy="",
        symbol=f.get("symbol", ""),
        day=date.min,
        direction=1,
        entry=leg,
        flags=[FLAG_UNMATCHED_FILL],
    )


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
