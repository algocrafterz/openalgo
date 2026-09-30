"""Exit driver: the shared Ladder as the only exit rule for runner-managed positions.

The backtest steps one strategy_core.exits.Ladder per open position, bar by bar. This driver
does the same in paper and live, so the exits that were scored are the exits that trade. It
holds no order logic of its own: a target fill becomes an EXIT Signal for main.dispatch_signal,
which reuses the tested booking, SL-cancel and retry code. The Ladder's stop reaches the broker
through signal.context["ladder_sl"] (main._replace_runner_sl override_sl), so there is one
stop rule.

A Ladder stop hit sends nothing: the broker's SL-M owns that exit and the tracker sees the
close. Only if the tracker still holds the position `safety_bars` bars later does the driver
ask the broker what is open, then reconcile (broker flat) or close for real (broker still
holds). Reconciling blindly would cancel a live stop and forget an open position.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import pandas as pd
from loguru import logger

from signal_engine.models import Direction, Signal
from signal_engine.strategy_core.exits import ExitPlan, Ladder
from signal_engine.timeutils import IST

_EPS = 1e-9

PLANS: dict[str, ExitPlan] = {
    "tp1_only": ExitPlan((1.0,), "none"),
    "ladder_30_35_35": ExitPlan((0.30, 0.35, 0.35), "tp_buffer", buffer_r=0.3),
}


def plan_for(name: str) -> ExitPlan:
    """The ExitPlan behind a config.yaml runner exit_plan name."""
    try:
        return PLANS[name]
    except KeyError:
        raise ValueError(f"unknown exit plan {name!r}; known: {', '.join(PLANS)}") from None


class PositionSource(Protocol):
    def find_position(self, symbol: str, strategy: str): ...


BrokerQty = Callable[[object], Awaitable[int]]  # open quantity at the broker; 0 flat, -1 unknown


@dataclass
class _State:
    ladder: Ladder
    key: tuple  # (entry_time, sig_id): a new trade in the same symbol gets a new ladder
    stop_hit: bool = False
    bars_since_stop: int = 0
    safety_sent: bool = False


def _to_float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _as_ist(t: dt.datetime) -> pd.Timestamp:
    ts = pd.Timestamp(t)
    return ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)


class ExitDriver:
    def __init__(
        self,
        *,
        strategy: str,
        plan: ExitPlan,
        positions: PositionSource,
        broker_qty: BrokerQty,
        interval_min: int,
        safety_bars: int,
    ) -> None:
        self.strategy = strategy
        self.plan = plan
        self._positions = positions
        self._broker_qty = broker_qty
        self._interval = pd.Timedelta(minutes=interval_min)
        self._safety_bars = safety_bars
        self._state: dict[str, _State] = {}
        self.unmanaged: set[str] = (
            set()
        )  # ladder could not be built; logged once, left to the tracker
        self._unmanaged_key: dict[str, tuple] = {}

    def holds(self, symbol: str) -> bool:
        """True while the tracker holds a position this driver's strategy opened."""
        return self._positions.find_position(symbol, self.strategy) is not None

    def ladder(self, symbol: str) -> Ladder | None:
        state = self._state.get(symbol)
        return None if state is None else state.ladder

    # ---- lifecycle ---------------------------------------------------------------------------

    def on_time_exit(self, symbol: str, price: float) -> None:
        """Keep the Ladder consistent with the tracker's time exit. Idempotent."""
        state = self._state.get(symbol)
        if state is not None:
            state.ladder.close_all(price, "TIME")

    async def on_bar(
        self, symbol: str, bar_open: pd.Timestamp, o: float, h: float, low: float
    ) -> list[Signal]:
        """Step the symbol's Ladder with one CLOSED bar; return the EXIT signals it produced."""
        pos = self._positions.find_position(symbol, self.strategy)
        if pos is None:
            self._state.pop(symbol, None)
            self.unmanaged.discard(symbol)
            self._unmanaged_key.pop(symbol, None)
            return []
        entry_time = _as_ist(pos.entry_time)
        if bar_open + self._interval <= entry_time:
            return []  # the bar closed before this position existed
        state = self._ensure(symbol, pos, entry_time)
        if state is None:
            return []
        if state.ladder.done:
            return await self._after_done(symbol, pos, state)
        return self._step(symbol, pos, state, o, h, low)

    # ---- internals ---------------------------------------------------------------------------

    def _ensure(self, symbol: str, pos, entry_time: pd.Timestamp) -> _State | None:
        key = (entry_time, getattr(pos, "sig_id", ""))
        state = self._state.get(symbol)
        if state is not None and state.key == key:
            return state
        self._state.pop(symbol, None)
        if self._unmanaged_key.get(symbol) == key:
            return None
        ladder = self._build(symbol, pos)
        if ladder is None:
            self.unmanaged.add(symbol)
            self._unmanaged_key[symbol] = key
            return None
        self.unmanaged.discard(symbol)
        self._unmanaged_key.pop(symbol, None)
        state = _State(ladder, key)
        self._state[symbol] = state
        return state

    def _build(self, symbol: str, pos) -> Ladder | None:
        fill = _to_float(pos.fill_price)
        if fill is None:
            fill = _to_float(pos.entry_price)
            logger.warning(f"{symbol}: fill price unknown, Ladder anchored to entry {fill}")
        ctx = pos.context if isinstance(pos.context, dict) else {}
        targets = [_to_float(pos.tp)]
        targets += [_to_float(ctx.get("tp2")), _to_float(ctx.get("tp3"))]
        needed = len(self.plan.fractions)
        usable = []
        for t in targets:
            if t is None:
                break
            usable.append(t)
        if fill is None or not (pos.sl and pos.sl > 0) or len(usable) < needed:
            logger.warning(
                f"{symbol}: cannot build a Ladder (fill={fill} sl={pos.sl} "
                f"targets={usable}, plan needs {needed}); position left to the tracker"
            )
            return None
        direction = 1 if pos.direction == Direction.LONG else -1
        return Ladder(direction, fill, float(pos.sl), tuple(usable), self.plan)

    def _step(self, symbol: str, pos, state: _State, o: float, h: float, low: float):
        ladder = state.ladder
        seen = len(ladder.fills)
        rem_before = ladder.remaining
        ladder.step(o, h, low)
        out: list[Signal] = []
        for fill in ladder.fills[seen:]:
            if fill.reason.startswith("TP"):
                left = rem_before - fill.frac
                pct = 1.0 if left <= _EPS else round(min(1.0, fill.frac / rem_before), 9)
                out.append(self._signal(pos, fill.reason, pct, ladder.sl_eff, fill.price))
                rem_before = left
            else:
                state.stop_hit = True
                logger.info(
                    f"{symbol}: Ladder stop {fill.reason} at {fill.price:.2f}; "
                    "the broker stop owns this exit"
                )
        return out

    async def _after_done(self, symbol: str, pos, state: _State) -> list[Signal]:
        if not state.stop_hit or state.safety_sent:
            return []
        state.bars_since_stop += 1
        if state.bars_since_stop < self._safety_bars:
            return []
        qty = await self._broker_qty(pos)
        if qty < 0:
            logger.warning(f"{symbol}: broker position unknown, safety check deferred")
            return []
        state.safety_sent = True
        if qty == 0:
            logger.warning(f"{symbol}: Ladder stop hit and broker is flat, reconciling tracker")
            return [self._signal(pos, "SL", 1.0, state.ladder.sl_eff)]
        logger.warning(f"{symbol}: Ladder stop hit but broker still holds {qty}; closing at market")
        return [self._signal(pos, None, 1.0, state.ladder.sl_eff)]

    def _signal(
        self, pos, tp_level: str | None, pct: float, sl: float, fill_price: float | None = None
    ) -> Signal:
        context = {"ladder_sl": float(sl), "source": "exit_driver"}
        if fill_price is not None:
            context["ladder_fill_price"] = float(fill_price)
        return Signal(
            strategy=pos.strategy,
            direction=Direction.EXIT,
            symbol=pos.symbol,
            entry=0.0,
            sl=float(sl),
            tp=float(pos.tp or 0.0),
            exchange=pos.exchange,
            product=pos.product,
            sig_id=getattr(pos, "sig_id", None) or None,
            tp_level=tp_level,
            exit_qty_pct=pct,
            context=context,
        )
