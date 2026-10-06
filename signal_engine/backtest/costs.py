"""Per-trade India cost model at the deployed notional (acceptance criteria G1.1, G1.3).

A flat round-trip figure in basis points (`types.india_intraday_bps` for intraday, a constant
for swing) hides two things the criteria ask for:

  * Brokerage is a flat Rs per order (capped at 0.03 percent at a generic discount broker, none
    at Flattrade), so the cost in bps depends on how large the position actually is. Live
    sizing is risk based, so a wide stop means a small position and a higher cost in bps; a
    tight stop means a large, cheap one.
  * Slippage is the uncertain part of cost and statutory charges are exact, so G1.3 stresses
    only slippage (doubled) and leaves the statutory schedule alone.

`CostModel` sizes each trade the way the live pipeline does (risk_per_trade x capital over
the stop distance, capped by leverage), prices it from the same `portfolio.costs` schedules
the Portfolio Backtester uses, with the deployment profile's broker (`CostModel.broker`), and
`recost` restates `r_net` from the trade's own `r_gross`. The recorded run restates its trades
once, at the source, so this model is the headline basis for every metric and gate. The flat
engine figure and other comparison scenarios (`cost_scenarios`) are reported beside it and
never drive a gate.

Deployed capital and sizing come from the caller (record_run reads `config.yaml`), never
from a default here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd

from portfolio.broker_fees import fees_for
from portfolio.costs import india_delivery, india_intraday
from signal_engine.backtest.metrics import clustered_t
from signal_engine.backtest.types import Trade
from signal_engine.research.intake.fingerprints import canonical_json, sha256_hex

PRODUCTS = ("MIS", "CNC")
EXCHANGES = ("NSE", "BSE")
MODEL_NAME = "statutory-per-trade"
#: Bump when the pricing logic changes in a way the rate table does not show.
COST_MODEL_VERSION = 1
#: G1.3 asks for total cost at these multiples of slippage.
STRESS_LEVELS = (1.0, 1.5, 2.0)
#: Positions are priced to the nearest rupee bucket; brokerage is the only lumpy line.
_NOTIONAL_BUCKET = 100.0


class CostModelUnavailable(ValueError):
    """The statutory cost model cannot be built or applied; a recorded run must not proceed."""


def _schedule(product: str, exchange: str, broker: str | None):
    """The `portfolio.costs` schedule for a product, with `broker`'s own brokerage."""
    build = india_intraday if product == "MIS" else india_delivery
    return build(exchange, broker=broker)


def _dp_rupees(product: str, schedule, broker: str | None, long: bool = True) -> float:
    """The broker's DP charge on a delivery sell, with GST, per round trip; 0 for intraday.

    `india_delivery` leaves it out (it is the broker's own charge, kept in
    `portfolio.broker_fees`), so it is added here the way `services.strategy_trade_enrichment`
    adds it to a live trade: only a CNC long has a delivery sell, so a short pays none.
    """
    if product != "CNC" or not long:
        return 0.0
    return fees_for(broker).dp_charge * (1.0 + schedule.tax_rate)


def broker_identity(broker: str | None) -> dict[str, Any]:
    """Which fee schedule prices `broker`: key, label, source, verified_on and `assumed`.

    A blank broker or one without a verified entry in `portfolio.broker_fees` is priced on the
    portfolio's default schedule and says `assumed: true`.
    """
    fees = fees_for(broker)
    return {
        "key": fees.key,
        "label": fees.label,
        "source": fees.source,
        "verified_on": fees.verified_on,
        "assumed": bool(fees.assumed),
    }


@lru_cache(maxsize=8192)
def _statutory_bps(
    product: str, exchange: str, broker: str | None, bucket: float, long: bool = True
) -> float:
    schedule = _schedule(product, exchange, broker)
    notional = bucket * _NOTIONAL_BUCKET
    charge = schedule.charge(buy_value=notional, sell_value=notional, orders=2)
    return (float(charge) + _dp_rupees(product, schedule, broker, long)) / notional * 1e4


#: Statutory components, in column order, with the `portfolio.costs` breakdown key each reads.
_STATUTORY_KEYS = (
    ("brokerage", "brokerage"),
    ("stt", "stt"),
    ("exchange", "exchange_txn"),
    ("gst", "tax"),
    ("sebi", "sebi"),
    ("stamp", "stamp_duty"),
)
COMPONENT_COLUMNS = tuple(f"cost_{name}_bps" for name, _ in _STATUTORY_KEYS) + (
    "cost_slippage_bps",
)
DECOMPOSITION_COLUMNS = (*COMPONENT_COLUMNS, "modeled_cost_bps")


@lru_cache(maxsize=8192)
def _component_bps(
    product: str, exchange: str, broker: str | None, bucket: float, long: bool = True
) -> tuple[float, ...]:
    """Statutory components in bps of entry notional, in `_STATUTORY_KEYS` order, then a residual.

    Same schedule, DP charge, orders and notional as `_statutory_bps`, but read from `breakdown()`
    so the lines stay separate. The last value is the residual: the schedule's own `slippage` line plus
    any charge key not named in `_STATUTORY_KEYS`. It is folded into the slippage column by
    the callers, so the components always sum to `_statutory_bps` even when a schedule gains a
    line.
    """
    schedule = _schedule(product, exchange, broker)
    notional = bucket * _NOTIONAL_BUCKET
    parts = schedule.breakdown(buy_value=notional, sell_value=notional, orders=2)
    named = tuple(float(parts[key]) for _, key in _STATUTORY_KEYS)
    residual = float(parts["total"]) - sum(named)
    # The DP charge is the broker's own, so it sits in the brokerage column.
    named = (named[0] + _dp_rupees(product, schedule, broker, long), *named[1:])
    return tuple(v / notional * 1e4 for v in (*named, residual))


@dataclass(frozen=True)
class CostModel:
    """Sizing and charge assumptions for one deployment.

    Only `fixed_notional` has a default. None sizes each trade by risk over its stop distance. A
    rupee amount sizes every trade to that notional, which is what a strategy with no stop needs.
    """

    capital: float
    risk_per_trade: float
    leverage: float
    product: str
    exchange: str
    slippage_bps_per_side: float
    fixed_notional: float | None = None
    #: The deployment profile's broker; None prices brokerage on the portfolio default schedule.
    broker: str | None = None

    def __post_init__(self) -> None:
        if self.product not in PRODUCTS:
            raise ValueError(f"product must be one of {PRODUCTS}, got {self.product!r}")
        if str(self.exchange).upper() not in EXCHANGES:
            raise CostModelUnavailable(
                f"exchange must be one of {EXCHANGES}, got {self.exchange!r}"
            )
        for name in ("capital", "risk_per_trade", "leverage"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if self.slippage_bps_per_side < 0:
            raise ValueError("slippage_bps_per_side must not be negative")
        if self.fixed_notional is not None and not self.fixed_notional > 0:
            raise ValueError("fixed_notional must be positive")

    def notional(self, trade: Trade) -> float:
        """Rupees of stock the live sizer would have bought for this trade."""
        if self.fixed_notional is not None:
            return float(self.fixed_notional)
        if not np.isfinite(trade.risk):
            raise CostModelUnavailable(
                f"{trade.symbol} has no stop, so it cannot be sized by risk; "
                "give the cost model a fixed_notional"
            )
        stop_fraction = trade.risk / trade.entry
        sized = self.capital * self.risk_per_trade / stop_fraction
        return float(min(sized, self.capital * self.leverage))

    def statutory_bps(self, trade: Trade) -> float:
        bucket = max(1.0, round(self.notional(trade) / _NOTIONAL_BUCKET))
        return _statutory_bps(
            self.product, self.exchange, self.broker, bucket, trade.direction != -1
        )

    def trade_cost_bps(self, trade: Trade, slippage_mult: float = 1.0) -> float:
        """Round-trip cost in bps of the entry notional: statutory plus two slippage legs."""
        return self.statutory_bps(trade) + 2.0 * self.slippage_bps_per_side * slippage_mult

    def components(self, trade: Trade, slippage_mult: float = 1.0) -> dict[str, float]:
        """Modelled round-trip cost split into lines, in bps of the entry notional.

        Keys are `cost_<line>_bps` for brokerage, stt, exchange, gst, sebi, stamp and slippage,
        plus `modeled_cost_bps` (their sum, equal to `trade_cost_bps` for the same arguments).
        """
        bucket = max(1.0, round(self.notional(trade) / _NOTIONAL_BUCKET))
        *statutory, residual = _component_bps(
            self.product, self.exchange, self.broker, bucket, trade.direction != -1
        )
        slippage = residual + 2.0 * self.slippage_bps_per_side * slippage_mult
        values = (*statutory, slippage)
        out = dict(zip(COMPONENT_COLUMNS, values, strict=True))
        out["modeled_cost_bps"] = float(sum(values))
        return out


def decompose_trades(trades, model: CostModel, slippage_mult: float = 1.0) -> pd.DataFrame:
    """One row per trade, in input order, with `DECOMPOSITION_COLUMNS` (float64)."""
    rows = list(trades)
    if not rows:
        return pd.DataFrame({c: np.empty(0, dtype=np.float64) for c in DECOMPOSITION_COLUMNS})
    entry = np.array([t.entry for t in rows], dtype=np.float64)
    risk = np.array([t.risk for t in rows], dtype=np.float64)
    if model.fixed_notional is not None:
        notional = np.full(len(rows), float(model.fixed_notional))
    else:
        sized = model.capital * model.risk_per_trade / (risk / entry)
        notional = np.minimum(sized, model.capital * model.leverage)
    buckets = np.maximum(1.0, np.round(notional / _NOTIONAL_BUCKET))
    longs = np.array([t.direction != -1 for t in rows], dtype=float)
    keys = np.column_stack([buckets, longs])
    uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
    table = np.array(
        [
            _component_bps(model.product, model.exchange, model.broker, float(b), bool(lg))
            for b, lg in uniq
        ],
        dtype=np.float64,
    )
    picked = table[inverse.reshape(-1)]
    statutory, residual = picked[:, :-1], picked[:, -1]
    slippage = residual + 2.0 * model.slippage_bps_per_side * slippage_mult
    data = {c: statutory[:, i] for i, c in enumerate(COMPONENT_COLUMNS[:-1])}
    data["cost_slippage_bps"] = slippage
    frame = pd.DataFrame(data, columns=list(COMPONENT_COLUMNS))
    frame["modeled_cost_bps"] = frame.sum(axis=1)
    return frame


def _restate(trades, bps_of) -> list[Trade]:
    """Copies of `trades` with the net result restated from each trade's own gross result.

    `bps_of(trade)` is that trade's round-trip cost in bps of the entry notional.
    """
    out = []
    for t in trades:
        bps = bps_of(t)
        if t.gross_ret is not None:
            # No stop: the result is a return on the entry price, restated directly.
            out.append(replace(t, net_ret=t.gross_ret - bps / 1e4))
        else:
            out.append(replace(t, r_net=t.r_gross - bps / 1e4 * t.entry / t.risk))
    return out


def recost(trades, model: CostModel, slippage_mult: float = 1.0) -> list[Trade]:
    """Copies of `trades` with `r_net` restated under `model`. Inputs are not modified."""
    return _restate(trades, lambda t: model.trade_cost_bps(t, slippage_mult))


def mean_total_bps(trades, model: CostModel) -> float:
    """Mean modelled round-trip cost in bps (statutory plus slippage), 0.0 for no trades.

    Rounded exactly as `cost_profile` rounds `mean_total_bps`, so the two always agree.
    """
    trades = list(trades)
    if not trades:
        return 0.0
    statutory = np.array([model.statutory_bps(t) for t in trades])
    return round(float(statutory.mean()) + 2.0 * model.slippage_bps_per_side, 3)


#: How the rupee figures in `cost_profile` are produced; stored with them so they are never read
#: as a portfolio result.
PNL_BASIS = (
    "modelled: each trade is sized independently to capital x risk per trade / stop fraction, "
    "capped at capital x leverage, with no limit on concurrent positions or total capital; "
    "net is after the modelled statutory charges and slippage"
)


def _inr(amount: float) -> str:
    """Rupees with Indian digit grouping, for example 1,00,000."""
    digits = f"{int(round(amount))}"
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail])


def _pnl_basis(model: CostModel) -> str:
    if model.fixed_notional is None:
        return PNL_BASIS
    return (
        f"modelled: every trade is a fixed notional of Rs {_inr(model.fixed_notional)} "
        "(capital per position), not sized by risk, with no limit on concurrent "
        "positions or total capital; net is after the modelled statutory charges and slippage"
    )


def rate_table(product: str, exchange: str, broker: str | None = None) -> dict[str, Any]:
    """The statutory rate lines the model prices trades with, as plain data.

    Read from the same `portfolio.costs` schedule (with `broker`'s own brokerage) as
    `_statutory_bps`, so the table cannot drift from the numbers: schedule name, tax rate (GST)
    and, per charge, its basis, rate, flat amount, per-order cap and whether the tax applies.
    """
    schedule = _schedule(product, exchange, broker)
    charges = {
        c.key: {"basis": c.basis, "rate": c.rate, "flat": c.flat, "cap": c.cap, "taxed": c.taxed}
        for c in schedule.charges
    }
    if product == "CNC":
        # Not a schedule line (see `_dp_rupees`), but it prices every delivery trade.
        charges["dp_charge"] = {
            "basis": "sell_scrip_per_round_trip",
            "flat": fees_for(broker).dp_charge,
            "taxed": True,
        }
    return {
        "schedule": schedule.name,
        "tax_label": schedule.tax_label,
        "tax_rate": schedule.tax_rate,
        "charges": charges,
    }


def rate_table_sha256(table: dict[str, Any]) -> str:
    """Version of a rate table: any change to a rate is a different hash."""
    return sha256_hex(canonical_json(table))


def model_rate_table_sha256(model: CostModel) -> str:
    """Rate-table version of the schedule `model` prices with (its broker included)."""
    return rate_table_sha256(rate_table(model.product, model.exchange, model.broker))


def cost_identity(model: CostModel | None) -> dict[str, Any] | None:
    """What identifies the primary cost basis in the cost fingerprint, or None without a model.

    The model's own settings, its broker key, the rate-table hash and the model version. Comparison
    scenarios and reference lines are deliberately absent: changing them is not a new trial.
    """
    if model is None:
        return None
    return {
        "model": asdict(model),
        "broker": broker_identity(model.broker)["key"],
        "rate_table_sha256": model_rate_table_sha256(model),
        "cost_model_version": COST_MODEL_VERSION,
    }


def _flat_label(bps: float) -> str:
    return f"flat_{bps:g}bps"


def _scenario_metrics(trades, restated, model: CostModel, bps: np.ndarray) -> dict[str, Any]:
    """Result of one cost scenario: net in bps (and R when every trade has a stop), rupees at the
    model notional, win rate and day-clustered t on net bps."""
    net_bps = np.array([t.net_bps for t in restated], dtype=float)
    days = np.array([pd.Timestamp(t.day).date().isoformat() for t in restated])
    out: dict[str, Any] = {"mean_round_trip_bps": round(float(bps.mean()), 3)}
    out["net_bps"] = round(float(net_bps.mean()), 4)
    notional = np.array([model.notional(t) for t in trades])
    if all(t.r_defined for t in trades):
        net_r = np.array([t.r_net for t in restated])
        out["net_r"] = round(float(net_r.mean()), 4)
        stop_over_entry = np.array([t.risk / t.entry for t in trades])
        net_rs = notional * net_r * stop_over_entry
        wins = net_r > 0
    else:
        net_ret = np.array([t.net_ret for t in restated])
        net_rs = notional * net_ret
        wins = net_ret > 0
    out["net_rs"] = round(float(net_rs.sum()), 2)
    out["win_rate"] = round(float(wins.mean()), 4)
    t_stat = clustered_t(net_bps, days)
    if np.isfinite(t_stat):
        out["t_stat"] = round(float(t_stat), 3)
    return out


def cost_scenarios(
    trades, model: CostModel, flat_bps: float, reference_bps: Sequence[float] = ()
) -> list[dict[str, Any]]:
    """The cost scenarios reported side by side, each computed per trade from the gross result.

    `primary` is `model` itself (profile broker, statutory charges, slippage) and drives every
    gate. `generic_discount_broker` is the same model with brokerage from the portfolio's default
    schedule. `flat_<n>bps` is one per reference level, and `legacy_flat` is the frozen engine
    figure `flat_bps`. Only `primary` is a gate input; the rest are report-only. The two broker
    scenarios also carry net at 1.5x and 2.0x slippage.
    """
    trades = list(trades)
    generic = replace(model, broker=None)
    scenarios: list[dict[str, Any]] = []
    for scenario_id, priced in (("primary", model), ("generic_discount_broker", generic)):
        bps = np.array([priced.trade_cost_bps(t) for t in trades])
        row = {
            "id": scenario_id,
            "definition": {
                "kind": "statutory",
                "broker": broker_identity(priced.broker)["key"],
                "rate_table_sha256": model_rate_table_sha256(priced),
                "slippage_bps_per_side": priced.slippage_bps_per_side,
            },
            **_scenario_metrics(trades, recost(trades, priced), priced, bps),
        }
        row["slippage_stress"] = {
            f"{m:.1f}": {
                k: v
                for k, v in _scenario_metrics(
                    trades,
                    recost(trades, priced, m),
                    priced,
                    np.array([priced.trade_cost_bps(t, m) for t in trades]),
                ).items()
                if k in ("mean_round_trip_bps", "net_bps", "net_r")
            }
            for m in STRESS_LEVELS[1:]
        }
        scenarios.append(row)
    levels = [(_flat_label(float(b)), float(b)) for b in reference_bps]
    levels.append(("legacy_flat", float(flat_bps)))
    for scenario_id, bps_value in levels:
        bps = np.full(len(trades), bps_value)
        restated = _restate(trades, lambda _t, b=bps_value: b)
        scenarios.append(
            {
                "id": scenario_id,
                "definition": {"kind": "flat", "round_trip_bps": bps_value},
                **_scenario_metrics(trades, restated, model, bps),
            }
        )
    return scenarios


def scenarios_sha256(scenarios: Sequence[Mapping[str, Any]]) -> str:
    """Hash of the scenario definitions (not their results)."""
    return sha256_hex(canonical_json([{"id": s["id"], "definition": s["definition"]}
                                      for s in scenarios]))


def cost_profile(
    trades, model: CostModel, flat_bps: float, reference_bps: Sequence[float] = ()
) -> dict[str, Any] | None:
    """The optional `cost_model` record block, or None for an empty trade list.

    A run whose trades have no stop reports its results in bps (`net_bps`, `net_bps_slip_2x`),
    not R, and sizes every trade to the model's `fixed_notional`.
    """
    trades = list(trades)
    if not trades:
        return None
    statutory = np.array([model.statutory_bps(t) for t in trades])
    table = rate_table(model.product, model.exchange, model.broker)
    slippage = 2.0 * model.slippage_bps_per_side
    scenarios = cost_scenarios(trades, model, flat_bps, reference_bps)
    costed = recost(trades, model)
    stressed_trades = recost(trades, model, slippage_mult=2.0)
    notional = np.array([model.notional(t) for t in trades])
    if all(t.r_defined for t in trades):
        net = np.array([t.r_net for t in costed])
        # Rupees: each trade is sized to `notional` and moves by (R x stop / entry) of it.
        stop_over_entry = np.array([t.risk / t.entry for t in trades])
        net_rs = notional * net * stop_over_entry
        cost_rs = notional * np.array([t.r_gross for t in trades]) * stop_over_entry - net_rs
        results = {
            "net_r": round(float(net.mean()), 4),
            "net_r_slip_2x": round(float(np.mean([t.r_net for t in stressed_trades])), 4),
        }
    else:
        net_ret = np.array([t.net_ret for t in costed])
        net_rs = notional * net_ret
        cost_rs = notional * np.array([t.gross_ret for t in trades]) - net_rs
        results = {
            "net_bps": round(float(net_ret.mean()) * 1e4, 4),
            "net_bps_slip_2x": round(float(np.mean([t.net_ret for t in stressed_trades])) * 1e4, 4),
        }
    return {
        "model": MODEL_NAME,
        "product": model.product,
        "exchange": model.exchange,
        "capital": model.capital,
        "risk_per_trade": model.risk_per_trade,
        "leverage": model.leverage,
        "slippage_bps_per_side": model.slippage_bps_per_side,
        "notional_basis": "fixed_notional" if model.fixed_notional is not None else "risk",
        "stress_levels": list(STRESS_LEVELS),
        "broker": broker_identity(model.broker),
        "cost_model_version": COST_MODEL_VERSION,
        "rate_table": table,
        "rate_table_sha256": rate_table_sha256(table),
        "flat_bps": float(flat_bps),
        "mean_statutory_bps": round(float(statutory.mean()), 3),
        "mean_total_bps": round(float(statutory.mean()) + slippage, 3),
        "total_bps_at": {
            f"{m:.1f}": round(float(statutory.mean()) + slippage * m, 3) for m in STRESS_LEVELS
        },
        **results,
        **({"fixed_notional": model.fixed_notional} if model.fixed_notional is not None else {}),
        "modelled_net_pnl_rs": round(float(net_rs.sum()), 2),
        "modelled_total_cost_rs": round(float(cost_rs.sum()), 2),
        "pnl_basis": _pnl_basis(model),
        "trades": len(trades),
        "scenarios": scenarios,
        "scenarios_sha256": scenarios_sha256(scenarios),
    }
