"""Trailing-stop exit driver for runner strategies that own their exits (exit_owner: strategy).

Paper/live counterpart of the backtest adapter's custom_exit: at each closed bar it replays
`strategy_core.ema_vwap_early.TrailState` over the position's bars, from the fill bar to the bar
that just closed, and asks whether that bar decided a trailing exit. The replay is stateless, so a
restart mid-trade rebuilds the same stop from the same bars.

Order handling is unchanged code: a decided exit becomes a full EXIT Signal (tp_level
"TRAILING_STOP") for main.dispatch_signal, which cancels the broker SL-M and sends the market
exit. The broker SL-M stays at the initial structural stop for intrabar protection and is never
moved here; a bar that touches it is left to the broker and the tracker.

DIAGNOSTICS: one JSON record per trade, whatever closed it, appended to
`logs/<tag>_diagnostics_<YYYY-MM-DD>.jsonl` and echoed to the engine log:
  TRAILING_STOP     the exit this driver sent
  INITIAL_STOP      the bar touched the broker SL-M (the broker and tracker book the close)
  FORCE_EXIT        still open on the last bar before the time exit (the tracker closes it)
  CLOSED_ELSEWHERE  the position left the tracker for another reason (bad fill, manual, ...)
The record carries the entry context (signal reason and time, stop distance in ATR, distance to
the swing) and the TrailState snapshot (MFE/MAE before and after the crossover, crossover price,
entry advantage, bars held). P&L stays in trades.db; join on sig_id
(analysis/evwap_early_report.py).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd
from loguru import logger

from signal_engine.models import Direction, Signal
from signal_engine.pipeline.context import RollingContext
from signal_engine.strategy_core.ema_vwap_early import (
    FORCE_EXIT,
    INITIAL_STOP,
    PAPER_TP_NONE,
    TRAILING_STOP,
    EarlyDisplacementParams,
    TrailState,
    replay_trail,
)
from signal_engine.strategy_core.trade_policy import MISSED_STOP_SAFETY
from signal_engine.timeutils import IST

DEFAULT_DIAGNOSTICS_DIR = Path(__file__).resolve().parents[1] / "logs"
CLOSED_ELSEWHERE = "CLOSED_ELSEWHERE"
#: The initial stop was touched but the position is still open SAFETY_BARS bars later (the paper
#: sandbox triggers SL-M from polled prices and can miss a short spike to the stop): closed at market.
INITIAL_STOP_SAFETY = MISSED_STOP_SAFETY
#: Bars after an initial-stop touch before the broker is asked what is open (as pipeline.service).
SAFETY_BARS = 2
#: Safety sends per position. main aborts a send whose SL cancel failed (the stop may be live), so a
#: later bar retries; after this many the 14:45 time exit (cancel all, then close) is the backstop.
MAX_SAFETY_ATTEMPTS = 3
#: entry-context keys copied into each trade record
_ENTRY_KEYS = (
    "signal_reason",
    "signal_time",
    "signal_price",
    "initial_stop",
    "stop_basis",
    "swing_level",
    "initial_risk",
    "stop_atr",
    "entry_to_swing_atr",
    "swing_age_bars",
    "wide_stop",
    "atr",
    "disp",
    "disp_prev",
    "disp_roc",
    "dist_vwap_atr",
    "dist_ema20_atr",
    "ema9_to_vwap_atr",
    "ema20_aligned",
    "market_state",
    "atr_regime",
    "pre_signal_path",
    "signals_today_before",
    "prev_signal_time",
    "prev_signal_direction",
    "bars_since_prev_signal",
)
#: input columns written into each path bar (raw facts for later research)
_PATH_COLS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "ema_f": "ema9",
    "ema_s": "ema20",
    "vwap": "vwap",
    "atr": "atr",
    "disp": "disp",
    "roc": "roc",
}


class PositionSource(Protocol):
    def find_position(self, symbol: str, strategy: str): ...


class TrailStrategy(Protocol):
    name: str
    params: EarlyDisplacementParams

    def inputs(self, df5m: pd.DataFrame) -> pd.DataFrame: ...


def _as_ist(t: dt.datetime) -> pd.Timestamp:
    ts = pd.Timestamp(t)
    return ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)


def _positive(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


@dataclass
class _Open:
    """What the driver remembers about a tracked position, so a close it did not send is recorded."""

    key: tuple  # (symbol, entry_time, sig_id)
    direction: int
    entry: float
    stop: float
    entry_time: pd.Timestamp
    context: dict
    fill_source: str  # "broker" (the confirmed entry fill) or "signal" (fill unknown, signal price)


def _round(v):
    if isinstance(v, float):
        return None if v != v else round(v, 4)  # NaN -> None
    return v


def _path(state: TrailState, inputs: pd.DataFrame) -> list[dict]:
    """The trade's bars, fill bar first: the TrailState facts plus the raw inputs of each bar."""
    out = []
    for step in state.path:
        bar = {k: _round(v) for k, v in step.items()}
        ts = pd.Timestamp(step["time"]) if step["time"] else None
        if ts is not None and ts in inputs.index:
            row = inputs.loc[ts]
            bar.update({name: _round(float(row[col])) for col, name in _PATH_COLS.items()})
        out.append(bar)
    return out


class TrailExitDriver:
    def __init__(
        self,
        *,
        strategy: TrailStrategy,
        context: RollingContext,
        positions: PositionSource,
        interval_min: int,
        manage_until_min: int | None = None,
        diagnostics_dir: Path | None = DEFAULT_DIAGNOSTICS_DIR,
        broker_qty=None,
        safety_bars: int = SAFETY_BARS,
    ) -> None:
        #: async (pos) -> open quantity at the broker (0 flat, -1 unknown); None disables the
        #: missed-initial-stop safety check
        self._broker_qty = broker_qty
        self._safety_bars = safety_bars
        self.strategy = strategy.name
        logger.info(
            f"{self.strategy} exits: trailing-stop driver, missed-stop safety "
            + (
                f"ON (broker check {safety_bars} bars after a stop touch, max {MAX_SAFETY_ATTEMPTS} attempts)"
                if broker_qty is not None
                else "OFF (no broker check)"
            )
        )
        self._strategy = strategy
        self._context = context
        self._positions = positions
        self._interval = pd.Timedelta(minutes=interval_min)
        #: minute of day at which the tracker's time exit takes over (None = never)
        self._manage_until = manage_until_min
        self._dir = diagnostics_dir
        self._last_stop: dict[tuple, float] = {}
        self._sent: set[tuple] = set()  # positions whose exit was sent
        self._recorded: set[tuple] = set()  # positions whose diagnostics record is written
        self._open: dict[str, _Open] = {}
        #: position key -> missed-stop safety state: attempts, last send bar, last broker answer
        self._safety: dict[tuple, dict] = {}
        #: closed positions whose record waits one bar (the close happened inside a forming bar)
        self._deferred: set[tuple] = set()
        self.unmanaged: set[str] = set()
        #: every record written this session, newest last (also on disk)
        self.records: list[dict] = []

    def holds(self, symbol: str) -> bool:
        """True while a position is open, or a closed one still owes its diagnostics record."""
        return (
            symbol in self._open or self._positions.find_position(symbol, self.strategy) is not None
        )

    def on_time_exit(self, symbol: str, price: float) -> None:
        """The tracker's time exit closes the position; nothing to keep in step here."""

    async def on_bar(
        self,
        symbol: str,
        bar_open: pd.Timestamp,
        o: float,
        h: float,
        low: float,
        c: float | None = None,
        frame: pd.DataFrame | None = None,
    ) -> list[Signal]:
        """Replay the trail up to this closed bar; return the EXIT signal it decided, if any."""
        pos = self._positions.find_position(symbol, self.strategy)
        if pos is None:
            self._closed(symbol, bar_open, frame)
            return []
        entry_time = _as_ist(pos.entry_time)
        if bar_open + self._interval <= entry_time:
            return []  # the bar closed before this position existed
        key = (symbol, entry_time, getattr(pos, "sig_id", "") or "")
        held = self._open.get(symbol)
        if held is not None and held.key != key:
            self._closed(symbol, bar_open, frame, defer=False)  # a new position replaced it
        if key in self._sent:
            return []
        held = self._remember(symbol, pos, key, entry_time)
        if held is None:
            return []
        if frame is None or frame.empty:
            logger.warning(
                f"{symbol}: trail driver got no bars for {bar_open:%H:%M}; stop not evaluated"
            )
            return []
        return await self._decide(symbol, pos, held, bar_open, frame)

    # ---- internals ---------------------------------------------------------------------------

    def _remember(self, symbol: str, pos, key: tuple, entry_time: pd.Timestamp) -> _Open | None:
        held = self._open.get(symbol)
        if held is not None and held.key == key:
            return held
        ctx = pos.context if isinstance(pos.context, dict) else {}
        stop = _positive(ctx.get("initial_stop")) or _positive(pos.sl)
        fill = _positive(pos.fill_price)
        entry = fill or _positive(pos.entry_price)
        if stop is None or entry is None:
            if symbol not in self.unmanaged:
                logger.warning(f"{symbol}: no entry ({entry}) or stop ({stop}); trail not managed")
            self.unmanaged.add(symbol)
            return None
        direction = 1 if pos.direction == Direction.LONG else -1
        held = _Open(
            key, direction, entry, stop, entry_time, dict(ctx), "broker" if fill else "signal"
        )
        self._open[symbol] = held
        return held

    def _replay(self, symbol: str, held: _Open, bar_open: pd.Timestamp, frame: pd.DataFrame):
        """(state, decided bar, inputs) over the fill bar .. bar_open; state None when not replayable."""
        d5, _ = self._context.window(symbol, frame, None)
        inputs = self._strategy.inputs(d5)
        fill_bars = inputs.index[inputs.index + self._interval > held.entry_time]
        if fill_bars.empty or fill_bars[0] > bar_open:
            return None, None, inputs
        try:
            state, decided = replay_trail(
                inputs,
                fill_bars[0],
                bar_open,
                held.direction,
                held.entry,
                held.stop,
                self._strategy.params,
            )
            return state, decided, inputs
        except ValueError as exc:
            # A fill already beyond the stop: the bad-fill guard in main closes it.
            if symbol not in self.unmanaged:
                logger.warning(f"{symbol}: trail not managed: {exc}")
            self.unmanaged.add(symbol)
            return None, None, inputs

    async def _decide(self, symbol, pos, held: _Open, bar_open, frame) -> list[Signal]:
        state, exit_bar, inputs = self._replay(symbol, held, bar_open, frame)
        if state is None:
            return []
        self._log_stop(symbol, held.key, state)
        if exit_bar is not None and state.exit is not None:
            if state.exit.reason == INITIAL_STOP:
                # The broker SL-M owns it and the tracker books the close (the record is written
                # then, by _closed). If the position is still here SAFETY_BARS bars later, the stop
                # did not execute: ask the broker, then reconcile or close at market.
                return await self._missed_stop(symbol, pos, held, state, exit_bar, bar_open, inputs)
            if exit_bar != bar_open:
                logger.warning(
                    f"{symbol}: trailing exit was decided on the {exit_bar:%H:%M} bar but is sent at "
                    f"{bar_open:%H:%M} (late or missed cycle)"
                )
            self._sent.add(held.key)
            self._record(symbol, held, state, state.exit.reason, exit_bar, inputs)
            return [self._signal(pos, state, exit_bar)]
        if self._last_managed(bar_open):
            self._record(symbol, held, state, FORCE_EXIT, bar_open, inputs)
        return []

    async def _missed_stop(
        self, symbol, pos, held, state, touch_bar, bar_open, inputs
    ) -> list[Signal]:
        """The bar data touched the initial stop but the tracker still holds the position.

        Waits SAFETY_BARS bars (the broker stop normally fills within seconds), then asks the broker:
        flat -> reconcile as the stop hit; still held -> close at market; unknown -> wait a bar. At
        most one send per bar and MAX_SAFETY_ATTEMPTS per position. The diagnostics record is
        written when the position actually closes (_closed), so the touch (bar data) and the
        execution (trades.db) stay separate facts.
        """
        bars_since = int((bar_open - touch_bar) / self._interval)
        safety = self._safety.setdefault(
            held.key, {"touch": touch_bar, "attempts": 0, "answers": []}
        )
        if self._broker_qty is None or bars_since < self._safety_bars:
            if self._last_managed(bar_open):
                # still open on the last bar before the time exit: the tracker closes it at 14:45
                self._record(symbol, held, state, FORCE_EXIT, bar_open, inputs)
            return []
        if safety["attempts"] >= MAX_SAFETY_ATTEMPTS:
            if safety["attempts"] == MAX_SAFETY_ATTEMPTS:
                safety["attempts"] += 1  # log once
                logger.critical(
                    f"{symbol}: initial stop touched at {touch_bar:%H:%M}, position still open after "
                    f"{MAX_SAFETY_ATTEMPTS} safety attempts; left to the time exit"
                )
            if self._last_managed(bar_open):
                self._record(symbol, held, state, FORCE_EXIT, bar_open, inputs)
            return []
        qty = await self._broker_qty(pos)
        safety["answers"].append(qty)
        if qty < 0:
            logger.warning(
                f"{symbol}: initial stop touched at {touch_bar:%H:%M}, broker position unknown; "
                "no order, retry next bar"
            )
            return []
        safety["attempts"] += 1
        safety["last_sent"] = bar_open
        if qty == 0:
            logger.warning(
                f"{symbol}: initial stop touched at {touch_bar:%H:%M} and broker is flat; reconciling tracker"
            )
            return [self._signal(pos, state, touch_bar, tp_level="SL")]
        logger.warning(
            f"{symbol}: initial stop {held.stop:.2f} touched at {touch_bar:%H:%M} but the broker still "
            f"holds {qty} after {bars_since} bars (stop not executed); closing at market"
        )
        return [self._signal(pos, state, bar_open, tp_level=INITIAL_STOP_SAFETY)]

    def _last_managed(self, bar_open: pd.Timestamp) -> bool:
        if self._manage_until is None:
            return False
        nxt = bar_open + self._interval
        return nxt.hour * 60 + nxt.minute >= self._manage_until

    def _closed(
        self, symbol: str, bar_open: pd.Timestamp, frame: pd.DataFrame | None, defer: bool = True
    ) -> None:
        """The position left the tracker: write its record if no other path did, then forget it.

        The closed bars explain most closes. Two cases they cannot, both seen on 2026-10-12:
        the tracker's time exit closing the position while the last managed bar is processed
        (recorded FORCE_EXIT), and a broker stop filling inside the bar that is still forming
        (the record waits one bar, so the replay includes the bar it closed in).
        """
        held = self._open.get(symbol)
        self.unmanaged.discard(symbol)
        if held is None:
            return
        if held.key not in self._recorded and frame is not None and not frame.empty:
            state, exit_bar, inputs = self._replay(symbol, held, bar_open, frame)
            if state is not None:
                if state.exit is not None:
                    reason, bar = state.exit.reason, exit_bar
                elif self._last_managed(bar_open):
                    reason, bar = FORCE_EXIT, bar_open
                elif defer and held.key not in self._deferred:
                    self._deferred.add(held.key)  # keep it held one more bar (holds() stays True)
                    return
                else:
                    reason, bar = CLOSED_ELSEWHERE, bar_open
                self._record(symbol, held, state, reason, bar, inputs)
        self._open.pop(symbol, None)
        self._deferred.discard(held.key)
        self._last_stop.pop(held.key, None)
        self._sent.discard(held.key)
        self._recorded.discard(held.key)
        self._safety.pop(held.key, None)

    def _record(
        self, symbol: str, held: _Open, state: TrailState, reason: str, bar, inputs: pd.DataFrame
    ) -> None:
        if held.key in self._recorded:
            return
        self._recorded.add(held.key)
        ctx = held.context
        prev = next((r for r in reversed(self.records) if r["symbol"] == symbol), None)
        entry_min = held.entry_time.hour * 60 + held.entry_time.minute
        record = {
            "strategy": self.strategy,
            "symbol": symbol,
            "sig_id": held.key[2],
            "direction": "LONG" if held.direction == 1 else "SHORT",
            "entry_time": str(held.entry_time),
            "entry_half_hour": f"{entry_min // 60:02d}:{(entry_min % 60) // 30 * 30:02d}",
            "fill_price": held.entry,
            "fill_price_source": held.fill_source,
            "trades_today_before": sum(1 for r in self.records if r["symbol"] == symbol),
            "prev_trade_direction": prev["direction"] if prev else None,
            "prev_trade_exit_reason": prev["exit_reason"] if prev else None,
            "prev_trade_exit_bar": prev["exit_decided_bar"] if prev else None,
            "prev_trade_mfe_r": prev["mfe_r"] if prev else None,
            "minutes_since_prev_exit": (
                (held.entry_time - pd.Timestamp(prev["exit_decided_bar"])).total_seconds() // 60
                if prev
                else None
            ),
            "exit_reason": reason,
            "exit_trigger": state.exit.trigger if state.exit is not None else "",
            "exit_stop": state.exit.stop if state.exit is not None else state.stop,
            # the price the exit was decided at: the stop level for a stop exit, else the last
            # closed bar (time exit fills at the next open). The real fill is in trades.db.
            "exit_price_reference": (
                state.exit.stop
                if state.exit is not None and reason in (INITIAL_STOP, TRAILING_STOP)
                else state.last_close
            ),
            "exit_decided_bar": str(bar),
            "minutes_in_trade": state.bars * int(self._interval.total_seconds() // 60),
            # the bar data touched the initial stop (a simulator/bar fact, not an execution); how the
            # stop was executed is the close row in trades.db (report: stop_execution)
            "stop_touch_bar": (
                state.path[-1]["time"]
                if state.exit is not None and state.exit.reason == INITIAL_STOP and state.path
                else None
            ),
            "safety_attempts": self._safety.get(held.key, {}).get("attempts", 0),
            "safety_broker_answers": self._safety.get(held.key, {}).get("answers", []),
            **{k: ctx[k] for k in _ENTRY_KEYS if k in ctx},
            **state.snapshot(),
            "path": _path(state, inputs),
        }
        self.records.append(record)
        line = json.dumps(record, default=str)
        logger.info(f"trail exit diagnostics {line}")
        if self._dir is None:
            return
        day = held.entry_time.date().isoformat()
        path = Path(self._dir) / f"{self.strategy.lower()}_diagnostics_{day}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            logger.exception(f"{symbol}: could not write the trail diagnostics record to {path}")

    def _log_stop(self, symbol: str, key: tuple, state) -> None:
        last = self._last_stop.get(key)
        if last is None or abs(state.stop - last) > 1e-9:
            self._last_stop[key] = state.stop
            logger.info(
                f"{symbol}: trail stop {state.stop:.2f} (initial {state.initial_stop:.2f}, "
                f"state {state.state}, activated {state.activated})"
            )

    def _signal(
        self, pos, state: TrailState, bar: pd.Timestamp, tp_level: str | None = None
    ) -> Signal:
        ex = state.exit
        context = {
            "source": "trail_driver",
            "exit_reason": tp_level or ex.reason,
            "exit_trigger": ex.trigger,
            "exit_stop": float(ex.stop),
            "exit_decided_bar": str(bar),
            **state.snapshot(),
        }
        return Signal(
            strategy=pos.strategy,
            direction=Direction.EXIT,
            symbol=pos.symbol,
            entry=0.0,
            sl=float(ex.stop),
            tp=PAPER_TP_NONE,  # take_profit none; main books the exit at the confirmed fill
            exchange=pos.exchange,
            product=pos.product,
            sig_id=getattr(pos, "sig_id", None) or None,
            tp_level=tp_level or ex.reason,
            exit_qty_pct=1.0,
            context=context,
        )
