"""G5.2 live caps: the strategy run with the live entry limits applied as signals arrive.

Design (owner-approved 2026-10-09):

  WHERE.      The limits are applied inside the engine run, not by filtering a finished trade
              list. Each symbol runs as `engine.simulate_steps(..., gated=True)`, which pauses
              at the point where a validated signal would become an order. A vetoed signal opens
              no position and the strategy keeps evaluating later bars by its normal rules.
  ORDER.      One chronological pass. A scheduler always resolves the pause with the earliest
              entry time (ties broken by a hash of symbol and time, so the order is deterministic
              and unrelated to ticker order). A symbol is paused only while flat, so every other
              symbol's history before that time is already complete, and a decision only reads
              history. There is no iteration, so nothing can oscillate, and the same input gives
              the same trades.
  RULES.      In the live order (config.yaml, mode_profiles.live; risk.py check_exposure):
                price band       the signal price must lie in [min_entry_price, max_entry_price]
                daily loss       realised gross losses of the day plus unrealised loss
                                 >= daily_loss_limit x day-start equity
                weekly loss      net realised loss of the calendar week (Monday start)
                                 >= weekly_loss_limit x day-start equity
                monthly loss     the same over the calendar month
                open positions   positions open at the entry time >= max_open_positions
                trades per day   entries already taken that IST day >= max_trades_per_day
              A limit of 0 is off. The band is applied first because a signal outside it never
              becomes a trade.
  SIZING.     A trade risks `risk_fraction` of day-start equity per R (nominal 1%, so slightly
              conservative: live sizing widens the denominator by the slippage factor).
              Day-start equity compounds from the day's net R as in the G5.1 account model.
              Trade results are net R under the cost model of the run.
  APPROXIMATIONS, stated in the evidence and never described as an exact live replay:
              pooled limits      live limits are pooled over all live strategies; this strategy
                                 may use the whole allowance (`pooled_allowance`), the most
                                 permissive reading
              unrealised loss    open positions are marked at the last completed 5-minute close
                                 of their own symbol, counting only positions in loss, as the
                                 tracker does; the live mark is a polled last price
              fills              an accepted signal fills at the next open exactly as in the
                                 normal run; no broker rejection is modelled
              slot count         a position occupies its slot through the end of its exit bar
  BASE CHECK. A pass with every limit off must reproduce the headline run trade for trade in
              count and net R. If it does not, the stage is not usable.
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from signal_engine.backtest import tickgrid
from signal_engine.backtest.cost_contribution import seal
from signal_engine.backtest.costs import recost
from signal_engine.backtest.engine import EntryRequest, simulate_steps
from signal_engine.backtest.perturbation import _same_result, trade_metrics
from signal_engine.backtest.types import Ctx
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

VERSION = 1
COMPLETE = "complete"
BASE_REPLAY_MISMATCH = "base_replay_mismatch"
UNSUPPORTED = "unsupported"
ERROR = "error"
STATES = (COMPLETE, BASE_REPLAY_MISMATCH, UNSUPPORTED, ERROR)
PRIMARY_SLOTS = 1
SLOT_PASSES = (1, 2, 3)
BAR_MINUTES = 5
CHUNK_SYMBOLS = 10
RULES = ("band", "daily", "weekly", "monthly", "slots", "trades_day")
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

ASSUMPTIONS = (
    "limits are applied inside the engine run as signals arrive; a vetoed signal opens no position and the symbol keeps trading by the strategy's normal rules",
    "one chronological pass, earliest entry time first, ties by a hash of symbol and time; deterministic, no iteration",
    "live order of checks: price band first, then daily, weekly and monthly loss, open positions, trades per day",
    "each trade risks risk_fraction of day-start equity per R; day-start equity compounds from the day's net R",
)
LIMITATIONS = (
    "approximation: live limits are pooled across all live strategies and this strategy is given the whole allowance, the most permissive reading",
    "approximation: unrealised loss marks each open position at the last completed 5-minute close of its own symbol and counts only positions in loss; the live figure is a polled last price",
    "approximation: nominal risk fraction; live sizing widens the risk denominator by the slippage factor, so actual risk per trade is slightly lower",
    "not modelled: broker rejections, margin, partial fills, and the strategies that would share the live account",
    "this is a simulation of the configured limits on historical signals; it is not proof that the strategy would have run feasibly in a live account",
)


class LiveCapsError(RuntimeError):
    """The capped run cannot be completed or trusted."""


@dataclass(frozen=True)
class LiveCaps:
    """The entry limits. 0 turns a limit off."""

    max_open_positions: int
    max_trades_per_day: int
    daily_loss_limit: float
    weekly_loss_limit: float
    monthly_loss_limit: float
    min_entry_price: float
    max_entry_price: float
    risk_fraction: float
    pooled_allowance: float = 1.0


def unlimited(risk_fraction: float = 0.01) -> LiveCaps:
    """Every limit off: the run must reproduce the headline run."""
    return LiveCaps(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, risk_fraction)


def slot_ceiling(
    risk_per_trade: float, mis_margin_pct: float, worst_case_sl_pct: float, slippage_factor: float
) -> int:
    """The capital-invariant open-position limit of the live formula (risk.py
    `_dynamic_max_open_positions` as capital grows): floor(1 / margin ratio per position)."""
    if worst_case_sl_pct <= 0 or mis_margin_pct <= 0:
        return 1
    ratio = risk_per_trade * mis_margin_pct / (worst_case_sl_pct * (1 + slippage_factor))
    if ratio <= 0:
        return 1
    return max(1, math.floor(1.0 / ratio))


def live_caps_from_config(
    path: str | Path = CONFIG_PATH, *, max_open_positions: int = PRIMARY_SLOTS
) -> tuple[LiveCaps, dict[str, Any]]:
    """The `live` profile of config.yaml as caps, and where each value came from."""
    cfg = yaml.safe_load(Path(path).read_text())
    prof = cfg["mode_profiles"]["live"]
    caps = LiveCaps(
        max_open_positions=int(max_open_positions),
        max_trades_per_day=int(prof["max_trades_per_day"]),
        daily_loss_limit=float(prof["daily_loss_limit"]),
        weekly_loss_limit=float(prof["weekly_loss_limit"]),
        monthly_loss_limit=float(prof["monthly_loss_limit"]),
        min_entry_price=float(prof["min_entry_price"]),
        max_entry_price=float(prof["max_entry_price"]),
        risk_fraction=float(cfg["sizing"]["risk_per_trade"]),
    )
    source = {
        "file": "signal_engine/config.yaml",
        "profile": "mode_profiles.live",
        "sizing": "sizing.risk_per_trade",
        "max_open_positions": "primary slot count 1 is the live formula's capital-invariant "
        "ceiling for the live strategy set's tightest stop; 2 and 3 are sensitivities",
        "live_config_max_open_positions": prof["max_open_positions"],
    }
    return caps, source


def _ist(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)


def _tie(symbol: str, ts: pd.Timestamp) -> str:
    return hashlib.sha1(f"{symbol}|{ts.value}".encode()).hexdigest()


@dataclass
class _Open:
    symbol: str
    entry_ns: int
    direction: int
    entry: float
    risk: float
    day_start: float


class Portfolio:
    """The live limits as a decision per entry request. Pure state; no I/O."""

    def __init__(
        self,
        caps: LiveCaps,
        cost_model: Any | None,
        mark: Callable[[str, int], float | None],
        bar_minutes: int = BAR_MINUTES,
    ):
        self.caps = caps
        self._cost = cost_model
        self._mark = mark
        self._bar_ns = bar_minutes * 60 * 10**9
        self.vetoes: Counter[str] = Counter()
        self.taken = 0
        self.peak_open = 0
        self._seq = 0
        self._open_heap: list[tuple[int, int]] = []
        self._open: dict[int, _Open] = {}
        self._closing: list[tuple[int, int, float, int, int, int]] = []
        self._daily_loss: dict[int, float] = {}
        self._week_net: dict[int, float] = {}
        self._month_net: dict[int, float] = {}
        self._entries_day: Counter[int] = Counter()
        self._day: int | None = None
        self._equity = 1.0
        self._day_start = 1.0
        self._day_r = 0.0

    # ---- decisions -------------------------------------------------------------------------

    def decide(self, req: EntryRequest) -> tuple[bool, str | None]:
        """(take, rule). `rule` names the limit that vetoed the entry."""
        t = req.entry_time.value
        ist = _ist(req.entry_time)
        day = ist.date().toordinal()
        if day != self._day:
            self._roll(day)
        c = self.caps
        if c.max_entry_price > 0 and not (c.min_entry_price <= req.price <= c.max_entry_price):
            return self._veto("band")
        self._release(t)
        if c.daily_loss_limit > 0 or c.weekly_loss_limit > 0 or c.monthly_loss_limit > 0:
            week, month = day - ist.weekday(), ist.year * 12 + ist.month
            base = self._day_start * c.pooled_allowance
            if c.daily_loss_limit > 0:
                loss = self._daily_loss.get(day, 0.0) + self._unrealised(t)
                if loss >= c.daily_loss_limit * base:
                    return self._veto("daily")
            if c.weekly_loss_limit > 0 and -self._week_net.get(week, 0.0) >= c.weekly_loss_limit * base:
                return self._veto("weekly")
            if c.monthly_loss_limit > 0 and -self._month_net.get(month, 0.0) >= c.monthly_loss_limit * base:
                return self._veto("monthly")
        if c.max_open_positions > 0 and len(self._open) >= c.max_open_positions:
            return self._veto("slots")
        if c.max_trades_per_day > 0 and self._entries_day[day] >= c.max_trades_per_day:
            return self._veto("trades_day")
        return True, None

    def ingest(self, trade: Any) -> None:
        """Record a completed accepted trade (called right after its symbol is resumed)."""
        r = float(recost([trade], self._cost)[0].r_net if self._cost is not None else trade.r_net)
        if not np.isfinite(r) or not np.isfinite(trade.risk) or trade.risk <= 0:
            raise LiveCapsError(f"{trade.symbol}: a trade without a defined stop cannot be sized")
        ist = _ist(pd.Timestamp(trade.entry_time))
        day = ist.date().toordinal()
        exit_end = pd.Timestamp(trade.exit_time).value + self._bar_ns
        pnl = r * self.caps.risk_fraction * self._day_start
        self._seq += 1
        heapq.heappush(self._open_heap, (exit_end, self._seq))
        self._open[self._seq] = _Open(
            trade.symbol,
            pd.Timestamp(trade.entry_time).value,
            int(trade.direction),
            float(trade.entry),
            float(trade.risk),
            self._day_start,
        )
        heapq.heappush(
            self._closing,
            (exit_end, self._seq, pnl, day, day - ist.weekday(), ist.year * 12 + ist.month),
        )
        self._entries_day[day] += 1
        self._day_r += r
        self.taken += 1
        self.peak_open = max(self.peak_open, len(self._open))

    # ---- state -----------------------------------------------------------------------------

    def _veto(self, rule: str) -> tuple[bool, str]:
        self.vetoes[rule] += 1
        return False, rule

    def _roll(self, day: int) -> None:
        if self._day is not None:
            self._equity *= 1.0 + self.caps.risk_fraction * self._day_r
        self._day, self._day_start, self._day_r = day, self._equity, 0.0

    def _release(self, t: int) -> None:
        while self._open_heap and self._open_heap[0][0] <= t:
            _, seq = heapq.heappop(self._open_heap)
            self._open.pop(seq, None)
        while self._closing and self._closing[0][0] <= t:
            _, _seq, pnl, day, week, month = heapq.heappop(self._closing)
            if pnl < 0:
                self._daily_loss[day] = self._daily_loss.get(day, 0.0) - pnl
            self._week_net[week] = self._week_net.get(week, 0.0) + pnl
            self._month_net[month] = self._month_net.get(month, 0.0) + pnl

    def _unrealised(self, t: int) -> float:
        total = 0.0
        for pos in self._open.values():
            if pos.entry_ns >= t:
                continue
            px = self._mark(pos.symbol, t)
            if px is None:
                continue
            move_r = pos.direction * (px - pos.entry) / pos.risk
            if move_r < 0:
                total += -move_r * self.caps.risk_fraction * pos.day_start
        return total


@dataclass
class CappedRun:
    trades: list[Any]
    vetoes: dict[str, int]
    taken: int
    peak_open: int


def trade_hash(trades: Sequence[Any]) -> str:
    """Order-independent fingerprint of a trade list, to compare runs."""
    rows = sorted(
        f"{t.symbol}|{pd.Timestamp(t.entry_time).value}|{pd.Timestamp(t.exit_time).value}|"
        f"{t.reason}|{round(float(t.r_net), 10) if np.isfinite(t.r_net) else 'nan'}"
        for t in trades
    )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def peak_margin(
    trades: Sequence[Any], cost_model: Any | None, bar_minutes: int = BAR_MINUTES
) -> dict[str, Any] | None:
    """Worst simultaneous MIS margin of a trade list, as a share of capital (G5.5).

    A position's margin is its order notional over the MIS leverage, in units of capital:
    `cost_model.notional / (capital x leverage)`, which does not depend on the capital level
    because sizing is linear in it. A position holds its margin from its entry fill through the
    end of its exit bar (the same hold the slot count uses); at equal times exits release before
    entries take. None without a cost model.
    """
    if cost_model is None:
        return None
    bar_ns = bar_minutes * 60 * 10**9
    scale = float(cost_model.capital) * float(cost_model.leverage)
    events: list[tuple[int, int, float]] = []
    for t in trades:
        m = float(cost_model.notional(t)) / scale
        events.append((pd.Timestamp(t.entry_time).value, 1, m))
        events.append((pd.Timestamp(t.exit_time).value + bar_ns, 0, m))
    events.sort()
    level, count, peak, peak_n, peak_at = 0.0, 0, 0.0, 0, None
    for ns, is_entry, m in events:
        if is_entry:
            level, count = level + m, count + 1
            if level > peak + 1e-12:
                peak, peak_n, peak_at = level, count, ns
        else:
            level, count = level - m, count - 1
    return {
        "peak_fraction": float(peak),
        "positions_at_peak": int(peak_n),
        "peak_time": None if peak_at is None else pd.Timestamp(peak_at, tz="UTC").tz_convert("Asia/Kolkata").isoformat(),
        "leverage": float(cost_model.leverage),
        "trades": len(trades),
    }


def run_capped(
    contexts: Mapping[str, tuple[Any, Ctx]],
    params: Any,
    run: Any,
    caps: LiveCaps,
    cost_model: Any | None = None,
) -> CappedRun:
    """One chronological capped pass. `contexts` maps symbol to (prepared strategy, Ctx); the
    strategy is copied, so contexts can be reused for another pass."""
    marks = {s: (ctx.index.asi8, ctx["Close"]) for s, (_st, ctx) in contexts.items()}

    def mark(symbol: str, t_ns: int) -> float | None:
        times, closes = marks[symbol]
        k = int(np.searchsorted(times, t_ns, side="left")) - 1
        return float(closes[k]) if k >= 0 else None

    portfolio = Portfolio(caps, cost_model, mark)
    gens: dict[str, Any] = {}
    lists: dict[str, list[Any]] = {}
    seen: dict[str, int] = {}
    waiting: dict[str, EntryRequest] = {}
    heap: list[tuple[int, str, str]] = []
    out: list[Any] = []

    def sync(symbol: str) -> None:
        trades = lists[symbol]
        for trade in trades[seen[symbol] :]:
            portfolio.ingest(trade)
        seen[symbol] = len(trades)

    def park(symbol: str, req: EntryRequest) -> None:
        waiting[symbol] = req
        lists[symbol] = req.trades
        heapq.heappush(heap, (req.entry_time.value, _tie(symbol, req.entry_time), symbol))

    for symbol in sorted(contexts):
        strategy, ctx = contexts[symbol]
        gen = simulate_steps(ctx, copy.deepcopy(strategy), params, run, None, gated=True)
        seen[symbol] = 0
        try:
            park(symbol, next(gen))
            gens[symbol] = gen
        except StopIteration as done:
            lists[symbol] = done.value
            sync(symbol)
            out.extend(done.value)

    while heap:
        _t, _tb, symbol = heapq.heappop(heap)
        req, gen = waiting.pop(symbol), gens[symbol]
        take, _rule = portfolio.decide(req)
        try:
            nxt = gen.send(take)
        except StopIteration as done:
            lists[symbol] = done.value
            sync(symbol)
            out.extend(done.value)
            del gens[symbol]
            continue
        sync(symbol)
        park(symbol, nxt)

    out.sort(key=lambda t: (pd.Timestamp(t.entry_time).value, t.symbol))
    return CappedRun(out, dict(portfolio.vetoes), portfolio.taken, portfolio.peak_open)


# ---- the recorded stage --------------------------------------------------------------------------


def _metrics(trades: Sequence[Any], cost_model: Any, oos_start: str | None) -> dict[str, Any]:
    restated = recost(trades, cost_model) if cost_model is not None else list(trades)
    m = trade_metrics(restated, oos_start)
    if restated:
        m["win_rate"] = float(np.mean([t.r_net > 0 for t in restated]))
    return m


def build_contexts(
    strategy: Any,
    params: Any,
    load_frames: Callable[[list[str]], Mapping[str, Any]],
    symbols: Sequence[str],
    chunk_size: int = CHUNK_SYMBOLS,
) -> dict[str, tuple[Any, Ctx]]:
    """Prepared (strategy, Ctx) per symbol, built exactly as the headline run builds them."""
    ordered = sorted(symbols)
    out: dict[str, tuple[Any, Ctx]] = {}
    for start in range(0, len(ordered), chunk_size):
        frames = dict(load_frames(ordered[start : start + chunk_size]))
        for symbol in sorted(frames):
            frame = frames[symbol]
            own = copy.deepcopy(strategy)
            factor = frame["price_factor"] if "price_factor" in frame.columns else None
            prepared = tickgrid.carry_price_factor(
                own.prepare_for(symbol, frame.copy(), params), factor
            )
            out[symbol] = (own, Ctx(prepared, symbol=symbol))
            frames[symbol] = None
    return out


def _finite_or_none(value: Any) -> Any:
    """Strict-JSON form: a non-finite float becomes None, as it will in the written file."""
    if isinstance(value, Mapping):
        return {k: _finite_or_none(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_or_none(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def seal_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """The evidence in strict-JSON form with a content hash over everything else.

    Non-finite numbers become None before hashing, so the hash is the same for the object in
    memory and for the file written from it; an edit to either is detectable."""
    body = _finite_or_none({k: v for k, v in evidence.items() if k != "content_sha256"})
    return {**body, "content_sha256": seal(body)}


def run_stage(**kw: Any) -> dict[str, Any]:
    """Run the base check and one capped pass per slot count; return the sealed evidence."""
    return seal_evidence(_run_stage(**kw))


def _run_stage(
    *,
    strategy: Any,
    params: Any,
    load_frames: Callable[[list[str]], Mapping[str, Any]],
    symbols: Sequence[str],
    run: Any,
    cost_model: Any,
    headline_trades: Sequence[Any],
    caps: LiveCaps,
    caps_source: Mapping[str, Any] | None = None,
    slot_passes: Sequence[int] = SLOT_PASSES,
    oos_start: str | None = None,
    chunk_size: int = CHUNK_SYMBOLS,
) -> dict[str, Any]:
    """Run the base check and one capped pass per slot count; return the evidence object."""
    evidence: dict[str, Any] = {
        "version": VERSION,
        "state": ERROR,
        "diagnostic_not_trials": True,
        "caps": asdict(caps),
        "caps_source": dict(caps_source or {}),
        "primary_slots": PRIMARY_SLOTS,
        "assumptions": list(ASSUMPTIONS),
        "limitations": list(LIMITATIONS),
    }
    try:
        head = {
            **trade_metrics(list(headline_trades), oos_start),
            "trade_sha256": trade_hash(headline_trades),
        }
        evidence["headline"] = head
        contexts = build_contexts(strategy, params, load_frames, symbols, chunk_size)
        guard = run_capped(contexts, params, run, unlimited(caps.risk_fraction), cost_model)
        guard_m = _metrics(guard.trades, cost_model, oos_start)
        evidence["base_check"] = {
            **guard_m,
            "trade_sha256": trade_hash(guard.trades),
            "margin": peak_margin(guard.trades, cost_model),
        }
        if not _same_result(guard_m, head):
            evidence.update(
                state=BASE_REPLAY_MISMATCH,
                reason="the unlimited gated pass does not reproduce the headline run",
            )
            return evidence
        passes = []
        for slots in slot_passes:
            capped = replace(caps, max_open_positions=int(slots))
            res = run_capped(contexts, params, run, capped, cost_model)
            m = _metrics(res.trades, cost_model, oos_start)
            passes.append(
                {
                    "max_open_positions": int(slots),
                    "primary": int(slots) == PRIMARY_SLOTS,
                    **m,
                    "vetoes": {rule: int(res.vetoes.get(rule, 0)) for rule in RULES},
                    "peak_open_positions": res.peak_open,
                    "trade_sha256": trade_hash(res.trades),
                    "margin": peak_margin(res.trades, cost_model),
                }
            )
        evidence.update(state=COMPLETE, passes=passes)
    except LiveCapsError as exc:
        evidence.update(state=UNSUPPORTED, reason=str(exc))
    except Exception as exc:  # noqa: BLE001 - recorded, never silently dropped
        logger.exception("live caps stage failed")
        evidence.update(state=ERROR, reason=f"{type(exc).__name__}: {exc}")
    return evidence


def validate_evidence(evidence: Any) -> list[str]:
    """Problems that make a live-caps evidence object unusable (empty when sound)."""
    if not isinstance(evidence, Mapping):
        return ["live caps evidence is not an object"]
    problems = []
    if evidence.get("state") not in STATES:
        problems.append(f"state must be one of {', '.join(STATES)}")
    if "content_sha256" in evidence:
        body = {k: v for k, v in evidence.items() if k != "content_sha256"}
        try:
            matches = evidence["content_sha256"] == seal(body)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            problems.append("content_sha256 does not match the payload")
    if evidence.get("state") == COMPLETE:
        passes = evidence.get("passes")
        if not isinstance(passes, list) or not passes:
            problems.append("a complete evidence object needs pass rows")
        else:
            primary = [p for p in passes if isinstance(p, Mapping) and p.get("primary")]
            if len(primary) != 1:
                problems.append("exactly one pass must be the primary case")
            for p in passes:
                if not isinstance(p, Mapping) or not isinstance(p.get("trades"), int):
                    problems.append("every pass needs an integer trade count")
                    break
        if not isinstance(evidence.get("base_check"), Mapping):
            problems.append("a complete evidence object needs its base check")
        if not evidence.get("limitations") or not evidence.get("assumptions"):
            problems.append("assumptions and limitations must be recorded")
    return problems
