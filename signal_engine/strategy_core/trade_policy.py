"""One trade policy for every mode (backtest engine, SimSink, paper runner, live).

BREAKOUT spec v1.0 items 15 to 19 decide which entries are allowed, how a bad fill is judged and when
the clock closes a position. Those rules live here and nowhere else, so a backtest, a paper run and a
live run cannot disagree about them. The module is pure: no I/O, no clock, no config reads. Callers
own the state value and pass it back in.

Counters count ACTUAL fills, not generated signals (owner decision 2026-10-08): a signal the policy,
the risk engine or the validator declines, an order the broker rejects and an order that does not fill
never move a counter. A fill moves the counter of its direction, including a fill a bad-fill guard
closes at once.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace

# Verdict codes. The names are the backtest ledger codes (`backtest/ledgers.py` CODE_INDEX) so every
# mode reports a declined signal with the same word.
OK = ""
POSITION_OPEN = "POSITION_OPEN"
MAX_TRADES_DAY = "MAX_TRADES_DAY"
DIRECTION_DISABLED = "DIRECTION_DISABLED"
DIRECTION_ALREADY_TAKEN = "DIRECTION_ALREADY_TAKEN"


@dataclass(frozen=True)
class TradePolicy:
    """The rules. `None` means no cap."""

    max_entries_per_direction_per_day: int | None = None
    max_trades_per_day: int | None = None
    allow_longs: bool = True
    allow_shorts: bool = True

    @classmethod
    def from_run(cls, run) -> TradePolicy:
        """A backtest `RunConfig` as a policy: `max_entries_per_direction`, else `one_trade_per_direction`."""
        cap = getattr(run, "max_entries_per_direction", None)
        if cap is None:
            cap = 1 if run.one_trade_per_direction else None
        return cls(
            max_entries_per_direction_per_day=cap,
            max_trades_per_day=run.max_trades_per_day,
            allow_longs=run.allow_longs,
            allow_shorts=run.allow_shorts,
        )


@dataclass(frozen=True)
class SymbolDay:
    """Per (strategy, symbol, trading day) state. Immutable: every transition returns a new value."""

    day: dt.date | None = None
    long_fills: int = 0
    short_fills: int = 0
    trades: int = 0
    open_direction: int = 0  # +1 long open, -1 short open, 0 flat
    pending: bool = False  # an entry order is armed and not yet filled


def roll(state: SymbolDay | None, day: dt.date) -> SymbolDay:
    """The state for `day`: a new day resets every counter and drops a pending order."""
    if state is None or state.day != day:
        return SymbolDay(day=day)
    return state


def blocked_by_position(position_open: bool, pending: bool) -> str:
    """At most one open position or unfilled entry order per symbol (no pyramiding)."""
    return POSITION_OPEN if (position_open or pending) else OK


def trades_capped(p: TradePolicy, trades: int) -> str:
    """The total daily trade cap. `None` applies no cap; a cap of 0 blocks every entry."""
    cap = p.max_trades_per_day
    return MAX_TRADES_DAY if (cap is not None and trades >= cap) else OK


def direction_verdict(p: TradePolicy, direction: int, long_fills: int, short_fills: int) -> str:
    """Direction switch and the per-direction daily count (`direction` is +1 or -1)."""
    if direction == 1:
        if not p.allow_longs:
            return DIRECTION_DISABLED
        fills = long_fills
    else:
        if not p.allow_shorts:
            return DIRECTION_DISABLED
        fills = short_fills
    cap = p.max_entries_per_direction_per_day
    return DIRECTION_ALREADY_TAKEN if (cap is not None and fills >= cap) else OK


def can_arm(p: TradePolicy, s: SymbolDay, direction: int) -> str:
    """Whether an entry signal may be armed now. Returns OK or the verdict code, first failure wins.

    Order: a position or order exists, the daily trade cap, then the direction rules. A reversal and a
    same-direction re-entry both need the symbol to be flat, which is the first test.
    """
    return (
        blocked_by_position(s.open_direction != 0, s.pending)
        or trades_capped(p, s.trades)
        or direction_verdict(p, direction, s.long_fills, s.short_fills)
    )


def rebuild(day: dt.date, filled_directions, open_direction: int = 0) -> SymbolDay:
    """The state for `day` rebuilt from the ledger after a restart.

    `filled_directions` holds +1 or -1 for every entry that FILLED today (rows of the ledger with a
    fill price; a declined, rejected or unfilled signal has none). `open_direction` is the direction
    of the position the tracker holds now, 0 when flat.
    """
    longs = sum(1 for d in filled_directions if d == 1)
    shorts = sum(1 for d in filled_directions if d == -1)
    return SymbolDay(
        day=day, long_fills=longs, short_fills=shorts, trades=longs + shorts, open_direction=open_direction
    )


# Bad-fill kinds. They are the ledger exit types of `db.EXIT_ENTRY_REJECTED_*`, so the live guard, the
# SimSink and the backtest name the same event the same way.
TP_OVERSHOT = "ENTRY_REJECTED_TP_OVERSHOT"
SLIPPAGE = "ENTRY_REJECTED_SLIPPAGE"
STOP_BREACHED = "ENTRY_REJECTED_STOP_BREACHED"


@dataclass(frozen=True)
class BadFill:
    violated: bool
    kind: str  # TP_OVERSHOT, STOP_BREACHED, SLIPPAGE or "" when the fill is acceptable
    slippage_r: float  # adverse slippage in units of R; negative when the fill was better


def entry_slippage_r(direction: int, signal_ref: float, sl: float, fill: float | None) -> float:
    """Adverse entry slippage in R (`R = |signal_ref - sl|`). 0.0 when the fill or R is unknown."""
    risk = abs(signal_ref - sl)
    if not fill or risk <= 0:
        return 0.0
    return direction * (fill - signal_ref) / risk


def bad_fill(
    direction: int, signal_ref: float, sl: float, tp1: float, fill: float | None, max_slippage_r: float
) -> BadFill:
    """Whether an actual entry fill is unusable (spec item 18). One function for every mode.

    A fill at or past TP1 (`direction x (fill - tp1) >= 0`) is a TP overshoot and is checked first. A fill at
    or beyond the stop (`direction x (fill - sl) <= 0`: a long that gaps down through its stop) is a distinct
    event, STOP_BREACHED (owner decision 2026-10-08). It is NOT a slippage case: for a long it is a lower,
    better price than the signal, so its slippage is negative, and it carries no fixed loss size. The R of
    the close comes from the actual fill against the entry. Adverse slippage above `max_slippage_r` is the
    last case; 0 or less disables it.
    """
    slip = entry_slippage_r(direction, signal_ref, sl, fill)
    # tp1 <= 0 (or NaN) is "no take-profit": there is no target to overshoot.
    if fill and tp1 and tp1 > 0 and direction * (fill - tp1) >= 0:
        return BadFill(True, TP_OVERSHOT, slip)
    if fill and sl and direction * (fill - sl) <= 0:
        return BadFill(True, STOP_BREACHED, slip)
    if max_slippage_r > 0 and slip > max_slippage_r:
        return BadFill(True, SLIPPAGE, slip)
    return BadFill(False, "", slip)


TIME_EXIT = "TIME_EXIT"
DATA_GAP = "DATA_GAP"

# Exit classes (BREAKOUT spec item 20, `docs/ACCOUNTING-INVARIANTS.md` I3). One mapping from an exit
# reason to the class that decides which view a trade belongs to.
STRATEGY_EXIT = "STRATEGY_EXIT"
BAD_FILL_CLOSE = "BAD_FILL_CLOSE"
_CLASS_BY_REASON = {
    "TIME_EXIT": TIME_EXIT,
    "EOD": TIME_EXIT,
    "DATA_GAP": DATA_GAP,
    "BAD_FILL_CLOSE": BAD_FILL_CLOSE,
    "SL_GAP_ENTRY": BAD_FILL_CLOSE,  # the older name of an entry closed at once through its stop
}


def classify_exit(reason: str) -> str:
    """STRATEGY_EXIT, TIME_EXIT, BAD_FILL_CLOSE or DATA_GAP for a backtest or SimSink exit reason."""
    return _CLASS_BY_REASON.get(reason, STRATEGY_EXIT)


def time_exit_verdict(bar_open_min: int, time_exit_min: int, squareoff_min: int) -> str:
    """What the clock does to a position still open at the start of the bar that opens at `bar_open_min`.

    Spec item 19: the first bar that opens at or after `time_exit_min` closes the position at its OPEN
    (TIME_EXIT). When that first bar opens at or after the broker square-off there was no usable bar
    in the window, so the close is the DATA_GAP exception: it is flagged, it is never a normal TIME_EXIT
    observation, and it stays out of parity counts and the v1.0 evidence. Returns "" before the time exit.
    """
    if bar_open_min < time_exit_min:
        return ""
    return TIME_EXIT if bar_open_min < squareoff_min else DATA_GAP


def on_arm(s: SymbolDay) -> SymbolDay:
    return replace(s, pending=True)


def on_unfilled(s: SymbolDay) -> SymbolDay:
    """The armed order did not fill (dropped at session end, skipped at the open, rejected)."""
    return replace(s, pending=False)


def on_fill(s: SymbolDay, direction: int) -> SymbolDay:
    """An entry filled (any quantity): the direction counter and the day total move, the symbol is open."""
    return replace(
        s,
        pending=False,
        trades=s.trades + 1,
        open_direction=direction,
        long_fills=s.long_fills + (1 if direction == 1 else 0),
        short_fills=s.short_fills + (1 if direction == -1 else 0),
    )


def on_flat(s: SymbolDay) -> SymbolDay:
    """The position is fully closed. Counters stay: a closed trade still counts as used."""
    return replace(s, open_direction=0)
