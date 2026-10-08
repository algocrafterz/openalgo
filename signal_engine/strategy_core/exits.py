"""Shared exit model: partial profit targets with stop moves.

ONE CODE PATH for exits. The backtest engine steps a `Ladder` per open position, bar by bar,
and the live/paper executors are meant to step the very same class on each new bar, so the
exit logic that is tuned in backtest is the exit logic that trades. Pure: no I/O, no clock.

Plan semantics (`ExitPlan`):
  fractions   share of the ORIGINAL position sold at target 1, 2, 3 ... (sum <= 1; whatever is
              left rides on the stop until a forced exit). A 0.0 leg sells nothing but still
              applies the stop rule, e.g. "move to breakeven at 1R, then target 2R".
  stop_rule   what happens to the stop after a target is hit (it only ever tightens):
                none        leave it
                breakeven   move to the entry
                tp_buffer   move to the hit target minus `buffer_r` x that target's distance
                            from entry (the live Pine/main.py runner-stop rule)
  same_bar_stop  False (default): a raised stop applies from the NEXT bar. True: the bar that
                filled the target may also take the raised stop - the pessimistic read.
  tp_fill     "target" (default): a touched target fills at the target price. "bar_close": it
              fills at the CLOSE of the bar that touched it (BREAKOUT spec v1.0 item 12; live
              sends a market exit after that bar closes).

Bar conventions match the shared engine: the stop is checked BEFORE targets (a bar spanning
both takes the stop), a bar opening beyond the stop fills at the open, and target fills are at
the target price, or the bar close under `tp_fill="bar_close"`. Anchors (entry for breakeven / buffer distances) are the ACTUAL fill.
"""

from __future__ import annotations

from dataclasses import dataclass

STOP_RULES = ("none", "breakeven", "tp_buffer")
TP_FILLS = ("target", "bar_close")
_EPS = 1e-9


@dataclass(frozen=True)
class ExitPlan:
    fractions: tuple[float, ...] = (1.0,)
    stop_rule: str = "none"
    buffer_r: float = 0.3
    same_bar_stop: bool = False
    tp_fill: str = "target"

    def __post_init__(self) -> None:
        if (
            not self.fractions
            or any(f < 0 for f in self.fractions)
            or sum(self.fractions) > 1 + _EPS
        ):
            raise ValueError(
                f"fractions must be non-negative and sum to at most 1, got {self.fractions}"
            )
        if self.stop_rule not in STOP_RULES:
            raise ValueError(f"stop_rule must be one of {STOP_RULES}, got {self.stop_rule!r}")
        if self.tp_fill not in TP_FILLS:
            raise ValueError(f"tp_fill must be one of {TP_FILLS}, got {self.tp_fill!r}")


#: The BREAKOUT v1.0 exit plan (spec item 12): the whole position at TP1, filled at the close of the
#: bar that touches it, no stop move. One definition for the backtest adapter and the runner.
TP1_ONLY = ExitPlan((1.0,), "none", tp_fill="bar_close")


@dataclass(frozen=True)
class Fill:
    price: float
    frac: float
    reason: str


class Ladder:
    """State of one open position under an ExitPlan."""

    def __init__(
        self, direction: int, entry: float, sl: float, targets: tuple[float, ...], plan: ExitPlan
    ):
        if len(plan.fractions) > len(targets):
            raise ValueError(f"plan has {len(plan.fractions)} legs but only {len(targets)} targets")
        self.direction = direction
        self.entry = entry
        self.sl_eff = sl
        self.targets = tuple(targets[: len(plan.fractions)])
        self.plan = plan
        self.fills: list[Fill] = []
        self.remaining = 1.0
        self._next = 0

    @property
    def done(self) -> bool:
        return self.remaining <= _EPS

    def _tighter(self, candidate: float) -> None:
        keep = max if self.direction == 1 else min
        self.sl_eff = keep(self.sl_eff, candidate)

    def _exit_rest(self, price: float, reason: str) -> None:
        if not self.done:
            self.fills.append(Fill(price, self.remaining, reason))
            self.remaining = 0.0

    def _after_target(self, target: float) -> None:
        rule = self.plan.stop_rule
        if rule == "breakeven":
            self._tighter(self.entry)
        elif rule == "tp_buffer":
            dist = abs(target - self.entry)
            self._tighter(target - self.direction * self.plan.buffer_r * dist)

    def step(self, o: float, h: float, low: float, c: float | None = None) -> None:
        """Advance one CLOSED bar (open, high, low, close). The close is required for tp_fill=bar_close."""
        if self.plan.tp_fill == "bar_close" and c is None:
            raise ValueError("tp_fill=bar_close needs the bar close")
        if self.done:
            return
        d = self.direction
        if (o <= self.sl_eff) if d == 1 else (o >= self.sl_eff):
            self._exit_rest(o, "SL_GAP")
            return
        if (low <= self.sl_eff) if d == 1 else (h >= self.sl_eff):
            self._exit_rest(self.sl_eff, "SL")
            return

        while self._next < len(self.targets):
            target = self.targets[self._next]
            if not ((h >= target) if d == 1 else (low <= target)):
                break
            frac = min(self.plan.fractions[self._next], self.remaining)
            if frac > _EPS:
                price = target if self.plan.tp_fill == "target" else float(c)
                self.fills.append(Fill(price, frac, f"TP{self._next + 1}"))
                self.remaining -= frac
            self._after_target(target)
            self._next += 1
            if self.done:
                return

        if self.plan.same_bar_stop and self._next > 0 and not self.done:
            if (low <= self.sl_eff) if d == 1 else (h >= self.sl_eff):
                self._exit_rest(self.sl_eff, "SL")

    def close_all(self, price: float, reason: str) -> None:
        """Forced exit of whatever remains (time exit, end of day). No-op once flat."""
        self._exit_rest(price, reason)
