"""SimSink: a simulated broker and tracker for replaying a strategy through the shared cycle.

It stands in for what the live system does after a Signal leaves the pipeline (risk gates, a
MARKET entry, the SL-M, the tracker) with the backtest engine's fill semantics, so a replay
through the real cycle and ExitDriver can be compared trade for trade with engine.simulate:

  entry     armed on the signal bar, filled at the NEXT bar's open (a fill through the stop is
            closed flat and tagged SL_GAP_ENTRY, as the engine does)
  stop      checked on every bar BEFORE targets: a bar opening beyond it fills at the open
            (SL_GAP), otherwise the stop price (SL). A stop the ExitDriver raises after a target
            applies from the next bar, and never loosens
  targets   the ExitDriver's EXIT signals, filled at the Ladder's price
  time exit the bar at or after rules.time_exit_min closes at its close

Hooks: on_bar runs first (fills, stops), then the driver steps the bar, then after_bar (time
exit). Entry gates are the engine's per-day rules, taken from RunConfig.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from signal_engine.backtest.types import RunConfig, Trade
from signal_engine.models import Direction, Signal
from signal_engine.strategy_core.trade_policy import (
    MAX_TRADES_DAY,
    TradePolicy,
    bad_fill,
    blocked_by_position,
    direction_verdict,
    time_exit_verdict,
    trades_capped,
)
from signal_engine.tracker import TrackedPosition

_EPS = 1e-9
_NOTIONAL_QTY = 1000  # tracked size is arbitrary; the sim works in fractions of the original


@dataclass(frozen=True)
class SimRules:
    max_trades_per_day: int
    one_trade_per_direction: bool
    allow_longs: bool
    allow_shorts: bool
    time_exit_min: int  # bar-open minutes after midnight at or after which the position closes
    cost_bps: float
    max_entries_per_direction: int | None = None  # spec item 15; overrides one_trade_per_direction
    max_entry_slippage_r: float = 0.0  # spec item 18; above 0 the bad-fill model is on
    time_exit_at_open: bool = False  # spec item 19
    squareoff_min: int = 900  # broker MIS square-off, bar-open minutes; at or after it a time exit is DATA_GAP

    @property
    def policy(self) -> TradePolicy:
        return TradePolicy(
            max_entries_per_direction_per_day=(
                self.max_entries_per_direction
                if self.max_entries_per_direction is not None
                else (1 if self.one_trade_per_direction else None)
            ),
            max_trades_per_day=self.max_trades_per_day,
            allow_longs=self.allow_longs,
            allow_shorts=self.allow_shorts,
        )

    @classmethod
    def from_run_config(cls, run: RunConfig, bar_min: int) -> SimRules:
        cap = run.mis_squareoff_min - run.squareoff_margin_min - bar_min
        return cls(
            max_trades_per_day=run.max_trades_per_day,
            one_trade_per_direction=run.one_trade_per_direction,
            allow_longs=run.allow_longs,
            allow_shorts=run.allow_shorts,
            time_exit_min=min(run.time_exit_min, cap),
            cost_bps=run.cost_bps,
            max_entries_per_direction=run.max_entries_per_direction,
            max_entry_slippage_r=run.max_entry_slippage_r,
            time_exit_at_open=run.time_exit_at_open,
            squareoff_min=run.mis_squareoff_min,
        )


@dataclass
class _Open:
    signal: Signal
    direction: int
    fill: float
    signal_price: float
    sl0: float
    sl: float  # the simulated broker stop, raised by the driver's ladder_sl
    tp: float
    risk: float
    entry_time: pd.Timestamp
    tracked: TrackedPosition
    remaining: float = 1.0
    fills: list[tuple[float, float, str]] = field(default_factory=list)


@dataclass
class _Day:
    day: dt.date | None = None
    trades: int = 0
    long_fills: int = 0
    short_fills: int = 0


_REJECT_TEXT = {
    "POSITION_OPEN": "a position or order is already open",
    MAX_TRADES_DAY: "trades per day cap reached",
}


def _tighter(direction: int, current: float, candidate: float) -> float:
    return max(current, candidate) if direction == 1 else min(current, candidate)


class SimSink:
    dispatches = False  # nothing here can reach a real broker

    def __init__(self, rules: SimRules, interval_min: int) -> None:
        self.rules = rules
        self._interval = interval_min
        self.trades: list[Trade] = []
        self._pending: dict[str, Signal] = {}
        self._open: dict[str, _Open] = {}
        self._days: dict[str, _Day] = {}
        self._last_bar: dict[str, tuple[pd.Timestamp, float]] = {}

    # ---- PositionSource for the ExitDriver ---------------------------------------------------

    def find_position(self, symbol: str, strategy: str) -> TrackedPosition | None:
        pos = self._open.get(symbol)
        return None if pos is None else pos.tracked

    # ---- ExecutionSink -----------------------------------------------------------------------

    async def submit(self, signal: Signal) -> None:
        if signal.direction == Direction.EXIT:
            self._exit(signal)
        else:
            self._arm(signal)

    async def on_bar(self, symbol, bar_open, o, h, low, c) -> None:
        self._roll_day(symbol, bar_open)
        previous = self._last_bar.get(symbol)
        self._last_bar[symbol] = (bar_open, c)
        pending = self._pending.pop(symbol, None)
        if pending is not None:
            self._fill(symbol, pending, bar_open, o)
        pos = self._open.get(symbol)
        if pos is None:
            return
        if self.rules.time_exit_at_open:
            # spec item 19: the clock closes the position at the open, before this bar's stop and target
            verdict = time_exit_verdict(
                bar_open.hour * 60 + bar_open.minute, self.rules.time_exit_min, self.rules.squareoff_min
            )
            if verdict == "TIME_EXIT":
                self._close(symbol, o, "TIME_EXIT", bar_open)
                return
            if verdict:
                self._close(symbol, previous[1] if previous else o, verdict, bar_open)
                return
        gapped = o <= pos.sl if pos.direction == 1 else o >= pos.sl
        hit = low <= pos.sl if pos.direction == 1 else h >= pos.sl
        if gapped:
            self._close(symbol, o, "SL_GAP", bar_open)
        elif hit:
            self._close(symbol, pos.sl, "SL", bar_open)

    async def after_bar(self, symbol, bar_open, o, h, low, c) -> None:
        if symbol not in self._open:
            return
        if self.rules.time_exit_at_open:
            return  # closed at the open in on_bar
        if bar_open.hour * 60 + bar_open.minute >= self.rules.time_exit_min:
            self._close(symbol, c, "TIME_EXIT", bar_open)

    def has_pending(self, symbol: str) -> bool:
        return symbol in self._pending

    def flush(self) -> None:
        """End of replay: close whatever is still open at its last close."""
        for symbol in list(self._open):
            bar_open, close = self._last_bar[symbol]
            # a position that outlives the replay saw no time-exit bar (spec item 19: DATA_GAP)
            self._close(symbol, close, "DATA_GAP" if self.rules.time_exit_at_open else "EOD", bar_open)

    # ---- entries -----------------------------------------------------------------------------

    def _roll_day(self, symbol: str, bar_open: pd.Timestamp) -> None:
        state = self._days.setdefault(symbol, _Day())
        if state.day is None:
            state.day = bar_open.date()
        elif state.day != bar_open.date():
            self._days[symbol] = _Day(day=bar_open.date())
            self._pending.pop(symbol, None)

    def _arm(self, signal: Signal) -> None:
        symbol = signal.symbol
        state = self._days.setdefault(symbol, _Day())
        d = 1 if signal.direction == Direction.LONG else -1
        verdict = (
            blocked_by_position(symbol in self._open, symbol in self._pending)
            or trades_capped(self.rules.policy, state.trades)
            or direction_verdict(self.rules.policy, d, state.long_fills, state.short_fills)
        )
        if verdict:
            why = _REJECT_TEXT.get(verdict, "long not allowed" if d == 1 else "short not allowed")
            logger.info(f"sim reject {symbol} {signal.direction.value}: {why}")
            return
        self._pending[symbol] = signal

    def _fill(self, symbol: str, signal: Signal, bar_open: pd.Timestamp, price: float) -> None:
        d = 1 if signal.direction == Direction.LONG else -1
        state = self._days[symbol]
        state.trades += 1
        if d == 1:
            state.long_fills += 1
        else:
            state.short_fills += 1
        tracked = TrackedPosition(
            symbol=symbol,
            strategy=signal.strategy,
            exchange=signal.exchange or "NSE",
            product=signal.product or "MIS",
            entry_price=price,
            quantity=_NOTIONAL_QTY,
            sl=signal.sl,
            tp=signal.tp,
            direction=signal.direction,
            fill_price=price,
            original_quantity=_NOTIONAL_QTY,
            entry_time=bar_open.to_pydatetime(),
            context=dict(signal.context),
            sig_id=signal.sig_id or "",
        )
        pos = _Open(
            signal=signal,
            direction=d,
            fill=price,
            signal_price=signal.entry,
            sl0=signal.sl,
            sl=signal.sl,
            tp=signal.tp,
            risk=abs(signal.entry - signal.sl),
            entry_time=bar_open,
            tracked=tracked,
        )
        self._open[symbol] = pos
        through = price <= signal.sl if d == 1 else price >= signal.sl
        if self.rules.max_entry_slippage_r > 0 and bad_fill(
            d, signal.entry, signal.sl, signal.tp, price, self.rules.max_entry_slippage_r
        ).violated:
            self._close(symbol, price, "BAD_FILL_CLOSE", bar_open)  # spec item 18
        elif through:
            self._close(symbol, price, "SL_GAP_ENTRY", bar_open)

    # ---- exits -------------------------------------------------------------------------------

    def _exit(self, signal: Signal) -> None:
        symbol = signal.symbol
        pos = self._open.get(symbol)
        if pos is None:
            logger.info(f"sim exit ignored for {symbol}: no open position")
            return
        bar_open, last_close = self._last_bar[symbol]
        level = signal.tp_level
        if level == "SL":
            self._close(symbol, pos.sl, "SL", bar_open)
            return
        if level is None:
            self._close(symbol, last_close, "EXIT", bar_open)
            return
        price = float(signal.context.get("ladder_fill_price", signal.tp))
        pct = signal.exit_qty_pct if signal.exit_qty_pct is not None else 1.0
        if pct >= 1.0 - _EPS:
            self._close(symbol, price, level, bar_open)
            return
        frac = pct * pos.remaining
        pos.fills.append((price, frac, level))
        pos.remaining -= frac
        pos.tracked.quantity = round(_NOTIONAL_QTY * pos.remaining)
        new_sl = signal.context.get("ladder_sl")
        if isinstance(new_sl, (int, float)) and new_sl > 0:
            pos.sl = _tighter(pos.direction, pos.sl, float(new_sl))
            pos.tracked.sl = pos.sl

    def _close(self, symbol: str, price: float, reason: str, bar_open: pd.Timestamp) -> None:
        pos = self._open.pop(symbol)
        if pos.remaining > _EPS:
            pos.fills.append((price, pos.remaining, reason))
            pos.remaining = 0.0
        weight = sum(f for _, f, _ in pos.fills)
        move = sum(
            f * ((p - pos.fill) if pos.direction == 1 else (pos.fill - p)) for p, f, _ in pos.fills
        )
        avg = sum(f * p for p, f, _ in pos.fills) / weight
        last_reason = pos.fills[-1][2]
        r_gross = move / pos.risk if pos.risk > 0 else 0.0
        cost_frac = self.rules.cost_bps / 100.0 / 100.0
        self.trades.append(
            Trade(
                symbol=symbol,
                day=bar_open.date(),
                direction=pos.direction,
                tag=str(pos.signal.context.get("trigger", "")),
                entry_time=pos.entry_time,
                entry=pos.fill,
                signal_price=pos.signal_price,
                sl=pos.sl0,
                tp=pos.tp,
                risk=pos.risk,
                exit_time=bar_open,
                exit=float(avg),
                reason="TP" if last_reason.startswith("TP") else last_reason,
                r_gross=r_gross,
                r_net=r_gross - cost_frac * pos.fill / pos.risk if pos.risk > 0 else 0.0,
                legs=len(pos.fills),
            )
        )
