# services/discretionary_charges.py
"""
Estimated charges for reconstructed discretionary trades (Stage 2).

Reuses the strategy book's cost model: ``services.strategy_trade_enrichment._schedule_for``
(NSE/BSE and MIS or CNC only) and ``_dp_charge``, over ``portfolio.costs`` and the broker
override in ``portfolio.broker_fees`` (so a Flattrade fill pays Flattrade's own brokerage).
Anything else (NFO, BFO, MCX, CDS, NRML, ...) gets ``charges=None`` and
``charges_status='unavailable'``: a price is never guessed.

How this differs from ``strategy_trade_enrichment.estimate_trade_costs``
-----------------------------------------------------------------------
* That function prices a closed-trade ROW as one entry order plus one exit order
  (``orders=2``) and then subtracts the entry order from the later legs of a position. Here the
  real ORDERS are known (fills carry an order id), so charges are computed per order:
  statutory lines are linear in value; brokerage is charged once per order through
  ``CostSchedule.breakdown(buy_value, 0, orders=1)`` (or ``(0, sell_value, 1)``), so the
  per-order brokerage cap applies to the order as a whole, not to a slice of it.
* Each order's charge is split pro rata by QUANTITY across the legs (closed trades, and the
  still-open position) that used its quantity. Entry charges sit on the open position
  (``entry_charges_carried``) and move onto trades as quantity closes, so a partial exit
  never charges the entry order twice: summed over trades and open positions the charge for
  an order is exactly its one-order charge.
* DP charge: Rs ``dp_charge`` x (1 + GST) once per (broker, account, symbol, sell session) on
  CNC LONG sells, as ``estimate_trade_costs`` does (it removes the DP from later legs of one
  day); here it goes on the first trade closed that session.
* No slippage (the schedules are built with slippage 0 and there is no signal price).
* Values are not rounded; rounding is the caller's presentation concern.

A fill with no order id is treated as an order of its own. Pure: no I/O.
"""

from __future__ import annotations

from services.discretionary_reconstruction import session_date
from services.strategy_trade_enrichment import _dp_charge, _schedule_for

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"

_META_KEYS = ("orders",)


def schedule_for(exchange: str, product: str, broker: str | None):
    """The cost schedule for an identity, or None when it is not modelled."""
    return _schedule_for({"exchange": exchange, "product": product, "broker": broker})


def order_charge(schedule, side: str, value: float) -> dict[str, float]:
    """Itemised charge of ONE order of ``value`` rupees on ``side`` (BUY or SELL)."""
    if str(side).upper() == "BUY":
        raw = schedule.breakdown(value, 0.0, orders=1)
    else:
        raw = schedule.breakdown(0.0, value, orders=1)
    return {k: float(v) for k, v in raw.items() if k not in _META_KEYS}


def _order_key(fill: dict) -> tuple:
    order_id = str(fill.get("order_id") or "")
    base = (fill["broker"], fill["account"], fill["exchange"], fill["symbol"], fill["product"])
    if order_id:
        return (*base, "order", order_id, str(fill["side"]).upper())
    return (*base, "fill", fill["id"], str(fill["side"]).upper())


def allocate_charges(result: dict, fills_by_id: dict[int, dict], expiry=None) -> None:
    """Fill in charges on ``result`` (output of ``reconstruct``), in place.

    Sets ``charge_allocated`` and ``charge_breakdown`` on every link, ``charges``,
    ``charges_status``, ``net_pnl`` and ``charges_breakdown`` on every trade, and
    ``entry_charges_carried`` on every position (0.0 once flat, None when unavailable).
    """
    links = result["links"]
    groups: dict[tuple, list[dict]] = {}
    for link in links:
        fill = fills_by_id[link["fill_ref"]]
        groups.setdefault(_order_key(fill), []).append(link)

    for group in groups.values():
        fill = fills_by_id[group[0]["fill_ref"]]
        schedule = schedule_for(fill["exchange"], fill["product"], fill["broker"])
        total_qty = sum(link["qty_used"] for link in group)
        if schedule is None or total_qty <= 0:
            for link in group:
                link["charge_allocated"] = None
                link["charge_breakdown"] = None
            continue
        value = sum(
            link["qty_used"] * float(fills_by_id[link["fill_ref"]]["price"]) for link in group
        )
        order = order_charge(schedule, fill["side"], value)
        for link in group:
            share = link["qty_used"] / total_qty
            link["charge_breakdown"] = {k: v * share for k, v in order.items() if k != "total"}
            link["charge_allocated"] = order["total"] * share

    _trade_totals(result, fills_by_id, expiry)

    for idx, pos in enumerate(result["positions"]):
        mine = [lk for lk in links if lk.get("pos_idx") == idx]
        if pos["status"] == "FLAT":
            pos["entry_charges_carried"] = 0.0
        elif any(lk["charge_allocated"] is None for lk in mine):
            pos["entry_charges_carried"] = None
        else:
            pos["entry_charges_carried"] = sum(lk["charge_allocated"] for lk in mine)


def _trade_totals(result: dict, fills_by_id: dict[int, dict], expiry) -> None:
    by_trade: dict[int, list[dict]] = {}
    for link in result["links"]:
        if "trade_idx" in link:
            by_trade.setdefault(link["trade_idx"], []).append(link)

    # DP: once per (broker, account, symbol, sell session), on the first trade closed.
    dp_done: set[tuple] = set()
    order = sorted(
        range(len(result["trades"])),
        key=lambda i: (result["trades"][i]["closed_at"], i),
    )
    for i in order:
        trade = result["trades"][i]
        legs = by_trade.get(i, [])
        schedule = schedule_for(trade["exchange"], trade["product"], trade["broker"])
        if schedule is None or any(lk["charge_allocated"] is None for lk in legs) or not legs:
            trade.update(
                charges=None, charges_status=STATUS_UNAVAILABLE, net_pnl=None, charges_breakdown={}
            )
            continue
        breakdown: dict[str, float] = {}
        for lk in legs:
            for name, amount in lk["charge_breakdown"].items():
                breakdown[name] = breakdown.get(name, 0.0) + amount
        total = sum(lk["charge_allocated"] for lk in legs)
        dp = _dp_charge(trade, schedule)
        if dp:
            dp_key = (
                trade["broker"],
                trade["account"],
                trade["symbol"],
                session_date(trade["closed_at"], expiry).isoformat(),
            )
            if dp_key in dp_done:
                dp = 0.0
            else:
                dp_done.add(dp_key)
        if dp:
            breakdown["dp"] = dp
            total += dp
        trade.update(
            charges=total,
            charges_status=STATUS_OK,
            net_pnl=trade["realized_pnl"] - total,
            charges_breakdown=breakdown,
        )


__all__ = [
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "allocate_charges",
    "order_charge",
    "schedule_for",
]
