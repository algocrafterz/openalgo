"""Cross-sectional selection baseline: what the strategy's names earn beyond the eligible pool.

NSE intraday has a large unconditional short drift, so a short strategy can look profitable
without its stock selection adding anything. For each session the strategy trades, this
compares its selected names against EVERY eligible symbol of that same session, run through
the strategy's own stop, target and exit logic by the real engine.

Definition, fixed here so runs stay comparable:

  KEY.  Each strategy trade has a key: (session, signal bar time, direction). The signal bar
  is the bar before the entry fill, so a replay enters at the same next-bar open.

  REPLAY.  For every key, `engine.simulate` runs on each symbol that has bars that session,
  with one scheduled signal at the key's bar. The signal comes from `Strategy.null_entry`
  (the strategy's stop and target rules on that symbol's own indicators, no selection test),
  under the same RunConfig: time exit, square-off cap, entry cutoff, `min_sl_pct`, skip-fill
  and exit plan. The pool includes the symbol the strategy picked.

  COST.  Each replay is restated with `costs.recost`, so it pays its own statutory cost.

  PAIRED.  null[key] is the mean over that key's filled replays. For each strategy trade the
  difference is its net (or gross) minus null[key]; `excess_t_stat` clusters the net
  differences by session, like the headline t. No random draws are involved.

  REPORT ONLY.  No gate reads this block. Indicators are prepared on each symbol's full
  history and only the sampled sessions are replayed, one chunk of symbols in memory at a
  time. Sessions are sampled whole, seeded, when keys x symbols exceeds `XS_MAX_REPLAYS`.
"""

from __future__ import annotations

import copy
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.baseline import _as_ns, _put
from signal_engine.backtest.costs import recost
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.metrics import clustered_t
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal
from utils.logging import get_logger

logger = get_logger(__name__)

METHOD = (
    "engine replay of the strategy's own stop and target rules on every symbol eligible in the "
    "same session, same signal bar and direction, each replay costed on the statutory model; "
    "excess is the strategy trade minus the mean of its key's replays"
)

#: Upper bound on estimated replays (keys x symbols); whole sessions are dropped beyond it.
XS_MAX_REPLAYS = 500_000

#: Symbols loaded and prepared at a time, which bounds the extra memory the baseline needs.
CHUNK_SYMBOLS = 10

NOT_IN_SET = "NOT_IN_OPPORTUNITY_SET"
NO_ENTRY_BAR = "NO_ENTRY_BAR"

#: (session ISO date, signal bar wall-clock ns, direction)
Key = tuple[str, int, int]


class SelectionNullContractError(RuntimeError):
    """A strategy's per-symbol prepare_for needs panel columns, so it cannot be replayed."""


def trade_key(trade: Any, bar_minutes: int = 5) -> Key | None:
    """The key of a strategy trade, or None when it has no entry time."""
    if trade.entry_time is None:
        return None
    signal_ns = _as_ns(trade.entry_time) - bar_minutes * 60 * 1_000_000_000
    return (str(trade.day), signal_ns, int(trade.direction))


def sample_sessions(keys: Iterable[Key], budget: int, seed: int = 0) -> list[str]:
    """Sessions kept under a budget of `budget` keys: a seeded shuffle, whole sessions only,
    taken in shuffled order until the next one would exceed the budget. Sorted on return."""
    per_session = Counter(day for day, _, _ in set(keys))
    order = sorted(per_session)
    np.random.default_rng(seed).shuffle(order)
    kept: list[str] = []
    used = 0
    for day in order:
        if used + per_session[day] > budget:
            break
        kept.append(day)
        used += per_session[day]
    return sorted(kept)


class _ScheduledNull(Strategy):
    """The wrapped strategy with one entry, at one bar, in one direction.

    Everything after entry (trail, target, custom exit, fill and exit hooks) is the wrapped
    strategy's own, so the replay is managed exactly as the strategy's trade would be.
    """

    def __init__(self, inner: Strategy, signal_bar: int | None, direction: int, min_sl_pct: float):
        self.inner = inner
        self.name = inner.name
        self.dynamic_target = bool(getattr(inner, "dynamic_target", False))
        self._bar = signal_bar
        self._dir = direction
        self._min_sl_pct = min_sl_pct
        #: Why no trade resulted, set while the engine runs; None when nothing went wrong.
        self.reason: str | None = None

    def prepare(self, df, p):
        return self.inner.prepare(df, p)

    def prepare_for(self, symbol, df, p):
        return self.inner.prepare_for(symbol, df, p)

    def reset_symbol(self, p) -> None:
        self.inner.reset_symbol(p)

    def reset_session(self, p) -> None:
        self.inner.reset_session(p)

    def entry(self, c: Ctx, i: int, p, direction: int) -> EntrySignal | None:
        if i != self._bar or direction != self._dir:
            return None
        sig = self.inner.null_entry(c, i, p, direction)
        if sig is None:
            self.reason = NOT_IN_SET
            return None
        close = float(c["Close"][i])
        risk = abs(close - sig.sl)
        if not risk > 0:
            self.reason = "RISK_INVALID"
        elif (sig.sl >= close) if direction == 1 else (sig.sl <= close):
            self.reason = "STOP_WRONG_SIDE"
        elif risk / close < self._min_sl_pct:
            self.reason = "STOP_TOO_TIGHT"
        return sig

    def trail(self, c, i, p, pos):
        return self.inner.trail(c, i, p, pos)

    def target(self, c, j, p, pos):
        return self.inner.target(c, j, p, pos)

    def custom_exit(self, c, i, p, pos):
        return self.inner.custom_exit(c, i, p, pos)

    def on_fill(self, c, i, p, pos) -> None:
        self.inner.on_fill(c, i, p, pos)

    def on_fill_skipped(self, c, i, p, sig) -> None:
        self.reason = "ENTRY_SKIPPED_BEYOND_LEVEL"
        self.inner.on_fill_skipped(c, i, p, sig)

    def on_exit(self, c, i, p, trade) -> None:
        self.inner.on_exit(c, i, p, trade)


def _wall_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Wall-clock nanoseconds of a bar index, matching `baseline._as_ns` on a timestamp."""
    if index.tz is not None:
        index = index.tz_localize(None)
    return index.to_numpy(dtype="datetime64[ns]").astype(np.int64)


def replay_symbol(
    inner: Strategy,
    symbol: str,
    prepared: pd.DataFrame,
    wanted: Mapping[str, Sequence[tuple[int, int, int]]],
    params: Any,
    run: Any,
) -> tuple[list[tuple[int, Any]], Counter, int]:
    """Replay one prepared symbol over the sessions in `wanted`.

    `wanted` maps a session to (key index, signal bar ns, direction) rows. Returns the filled
    replays as (key index, Trade), the count of unfilled replays by reason, and the number of
    replays attempted. A session the symbol has no bars on is not an attempt: the symbol is
    not in that session's pool.
    """
    wall = _wall_ns(prepared.index)
    starts = np.flatnonzero(prepared["new_session"].to_numpy())
    ends = np.append(starts[1:], len(prepared))
    day_at = prepared["day"].to_numpy()[starts]
    span = {str(d): (int(a), int(b)) for d, a, b in zip(day_at, starts, ends, strict=True)}
    # The slice starts after the history the indicators already used, so no extra warm-up.
    replay_run = run.with_(warmup_bars=0)
    filled: list[tuple[int, Any]] = []
    skipped: Counter = Counter()
    attempts = 0
    for day, rows in wanted.items():
        if day not in span:
            continue
        lo, hi = span[day]
        ctx = Ctx(prepared.iloc[lo:hi], symbol=symbol)
        session_wall = wall[lo:hi]
        for key_idx, signal_ns, direction in rows:
            attempts += 1
            at = int(np.searchsorted(session_wall, signal_ns))
            if at >= len(session_wall) or session_wall[at] != signal_ns:
                skipped[NO_ENTRY_BAR] += 1
                continue
            wrapper = _ScheduledNull(inner, at, direction, replay_run.min_sl_pct)
            trades = simulate(ctx, wrapper, params, replay_run)
            if trades:
                filled.append((key_idx, trades[0]))
            else:
                skipped[wrapper.reason or "NOT_FILLED"] += 1
    return filled, skipped, attempts


def _chunks(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def cross_sectional_baseline(
    trades: Sequence[Any],
    load_frames: Callable[[list[str]], Mapping[str, pd.DataFrame]],
    strategy: Strategy,
    params: Any,
    run: Any,
    cost_model: Any,
    *,
    symbols: Sequence[str],
    seed: int = 0,
    max_replays: int = XS_MAX_REPLAYS,
    chunk_size: int = CHUNK_SYMBOLS,
    bar_minutes: int = 5,
) -> dict[str, Any] | None:
    """The `baseline_cross_sectional` block for `trades`, or None when nothing can be paired.

    `load_frames(chunk)` returns the baseline frames (run eligibility and warm-up already
    applied) for a list of symbols. `trades` are the run's trades on the statutory basis.
    """
    if not getattr(strategy, "supports_selection_null", False):
        return None
    symbols = sorted(symbols)
    population = len(trades)
    keyed = [(t, trade_key(t, bar_minutes)) for t in trades]
    keyed = [(t, k) for t, k in keyed if k is not None]
    all_keys = {k for _, k in keyed}
    population_sessions = len({k[0] for k in all_keys})
    budget = max_replays // max(1, len(symbols))
    kept_days = set(sample_sessions(all_keys, budget, seed))
    keys = sorted(k for k in all_keys if k[0] in kept_days)
    if not keys:
        return None
    key_index = {k: n for n, k in enumerate(keys)}
    wanted: dict[str, list[tuple[int, int, int]]] = {}
    for n, (day, signal_ns, direction) in enumerate(keys):
        wanted.setdefault(day, []).append((n, signal_ns, direction))

    inner = copy.deepcopy(strategy)
    gross_sum = np.zeros(len(keys))
    net_sum = np.zeros(len(keys))
    filled_n = np.zeros(len(keys), dtype=int)
    skip_reasons: Counter = Counter()
    replays = 0
    for chunk in _chunks(symbols, chunk_size):
        frames = dict(load_frames(chunk))
        for symbol in sorted(frames):
            try:
                prepared = inner.prepare_for(symbol, frames[symbol], params)
            except KeyError as exc:
                raise SelectionNullContractError(
                    f"{type(strategy).__name__}.prepare_for({symbol}) failed on missing column "
                    f"{exc}. The null_entry hook contract requires the per-symbol prepare_for "
                    "to work without panel columns (prepare_panel is never run for the "
                    "replays); build the stop and target from the symbol's own data."
                ) from exc
            filled, skipped, attempts = replay_symbol(inner, symbol, prepared, wanted, params, run)
            frames[symbol] = None
            del prepared
            replays += attempts
            skip_reasons.update(skipped)
            if not filled:
                continue
            restated = recost([t for _, t in filled], cost_model)
            for (idx, _), costed in zip(filled, restated, strict=True):
                gross_sum[idx] += costed.gross_bps
                net_sum[idx] += costed.net_bps
                filled_n[idx] += 1
        del frames

    has_null = filled_n > 0
    null_gross = np.where(has_null, gross_sum / np.maximum(filled_n, 1), np.nan)
    null_net = np.where(has_null, net_sum / np.maximum(filled_n, 1), np.nan)
    used = [(t, key_index[k]) for t, k in keyed if k in key_index and has_null[key_index[k]]]
    in_sample = sum(1 for _, k in keyed if k in key_index)
    if not used:
        return None
    t_gross = np.array([t.gross_bps for t, _ in used])
    t_net = np.array([t.net_bps for t, _ in used])
    n_gross = np.array([null_gross[i] for _, i in used])
    n_net = np.array([null_net[i] for _, i in used])
    sessions = np.array([str(t.day) for t, _ in used])

    out: dict[str, Any] = {
        "method": METHOD,
        "seed": seed,
        "null_pool": "all symbols with bars in the session, including the traded symbol",
        "sessions": len(kept_days),
        "sessions_population": population_sessions,
        "sampled": len(kept_days) < population_sessions,
        "trades": len(used),
        "population_trades": population,
        "skipped": in_sample - len(used),
        "replays": replays,
        "replays_filled": int(filled_n.sum()),
        "replay_skips": dict(sorted(skip_reasons.items())),
    }
    _put(out, "pool_median_symbols", float(np.median(filled_n[has_null])), 1)
    _put(out, "strategy_gross_bps", t_gross.mean())
    _put(out, "strategy_net_bps", t_net.mean())
    _put(out, "null_gross_bps", n_gross.mean())
    _put(out, "null_net_bps", n_net.mean())
    _put(out, "null_gross_t_stat", clustered_t(n_gross, sessions), 3)
    _put(out, "excess_gross_bps", (t_gross - n_gross).mean())
    _put(out, "excess_net_bps", (t_net - n_net).mean())
    _put(out, "excess_t_stat", clustered_t(t_net - n_net, sessions), 3)
    if not math.isfinite(out.get("excess_net_bps", float("nan"))):
        return None
    logger.info(
        f"Cross-sectional baseline: {out['trades']} trades, {replays} replays "
        f"({out['replays_filled']} filled), excess net {out.get('excess_net_bps')} bps"
    )
    return out
