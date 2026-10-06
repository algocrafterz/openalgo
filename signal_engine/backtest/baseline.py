"""Random-entry baseline: what the same names earn when entered on arbitrary days.

A long-only strategy tested through a rising market earns the market. Dividend growth
showed +380 bps a trade and was still rejected because random entry with the same holding
period earned more. This puts that comparison in every run record so a result that is only
beta cannot pass for skill.

Definition, fixed here so runs stay comparable:

  MATCHED PER TRADE.  Each trade is paired with random entries on the SAME symbol, held for
  the SAME number of bars, in the SAME direction, with that trade's own round-trip cost
  deducted (the gap between its gross and net bps, so a per-trade cost model is honoured).
  Hold is counted in bars, so a Friday-to-Monday hold is one bar.

  OPEN TO CLOSE.  The baseline enters at a bar's open and leaves at a later bar's close,
  which is how the daily swing engine fills. A hold of zero bars (in and out the same day)
  is valid and matched, so no trade is dropped for being short; dropping them would
  compare the baseline on a biased subset. The strategy's own fills follow its engine
  rules (stops, targets), so `excess_net_bps` is a comparison of averages, not a claim
  about any single fill. `trade_net_bps` is the strategy's mean over the SAME matched
  trades, so the excess always compares like with like.

  EXCESS IS PAIRED.  `excess_net_bps` is the mean of (trade net bps minus that trade's own
  baseline net bps), and `excess_t_stat` clusters those pairs by day, like the headline t.

  DETERMINISTIC.  Trades are drawn in a fixed order from a seeded generator, so the same
  inputs always give the same answer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.metrics import clustered_t

#: Random entries drawn per trade. Averaging these is what removes the luck of one date.
DRAWS_PER_TRADE = 20

METHOD = (
    "same-symbol random entry, same holding period in bars, same direction, "
    "open to close, the matched trade's own round-trip cost deducted"
)


def _day_numbers(values: Any) -> np.ndarray:
    """Calendar days as integers, ignoring time zone and time of day."""
    index = pd.DatetimeIndex(values)
    if index.tz is not None:
        index = index.tz_localize(None)
    return index.normalize().values.astype("datetime64[D]").astype(np.int64)


def _put(target: dict[str, Any], key: str, value: float, digits: int = 4) -> None:
    if math.isfinite(value):
        target[key] = round(float(value), digits)


def random_entry_baseline(
    trades: Sequence[Any],
    frames: Mapping[str, pd.DataFrame],
    *,
    draws: int = DRAWS_PER_TRADE,
    seed: int = 0,
) -> dict[str, Any] | None:
    """The matched random-entry result for `trades`, or None when no trade can be matched.

    `frames` maps symbol to a frame with a `Close` column and a daily `DatetimeIndex`.
    A trade is skipped, and counted in `skipped`, when its symbol has no frame or its hold
    is too long for the history to sample `draws` different start bars.
    """
    prepared: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    matched: list[tuple[Any, int, np.ndarray, np.ndarray]] = []
    skipped = 0
    for trade in sorted(trades, key=lambda t: (t.symbol, str(t.entry_time), str(t.exit_time))):
        frame = frames.get(trade.symbol)
        if frame is None or trade.entry_time is None or trade.exit_time is None:
            skipped += 1
            continue
        if trade.symbol not in prepared:
            prepared[trade.symbol] = (
                frame["Open"].to_numpy(dtype=float),
                frame["Close"].to_numpy(dtype=float),
                _day_numbers(frame.index),
            )
        opens, closes, days = prepared[trade.symbol]
        start = int(np.searchsorted(days, _day_numbers([trade.entry_time])[0], side="left"))
        end = int(np.searchsorted(days, _day_numbers([trade.exit_time])[0], side="left"))
        hold = end - start
        if hold < 0 or len(closes) - hold < draws:
            skipped += 1
            continue
        matched.append((trade, hold, opens, closes))
    if not matched:
        return None

    rng = np.random.default_rng(seed)
    base_gross = np.empty(len(matched))
    trade_net = np.empty(len(matched))
    trade_cost = np.empty(len(matched))
    for i, (trade, hold, opens, closes) in enumerate(matched):
        entries = rng.integers(0, len(closes) - hold, size=draws)
        # A trade that exited at an open is matched against an open-to-open baseline.
        exits = opens if getattr(trade, "exit_at_open", False) else closes
        moves = (exits[entries + hold] - opens[entries]) / opens[entries] * 1e4
        base_gross[i] = trade.direction * moves.mean()
        trade_net[i] = trade.net_bps
        trade_cost[i] = trade.gross_bps - trade.net_bps
    base_net = base_gross - trade_cost
    diff = trade_net - base_net

    out: dict[str, Any] = {
        "method": METHOD,
        "draws_per_trade": draws,
        "seed": seed,
        "trades": len(matched),
        "skipped": skipped,
    }
    _put(out, "gross_bps", base_gross.mean())
    _put(out, "net_bps", base_net.mean())
    _put(out, "trade_net_bps", trade_net.mean())
    _put(out, "excess_net_bps", diff.mean())
    trade_days = np.array([str(m[0].day) for m in matched])
    _put(out, "excess_t_stat", clustered_t(diff, trade_days), 3)
    return out


METHOD_INTRADAY = (
    "same-symbol random entry, same holding period in bars, same direction, same session, "
    "open of the entry bar to close of the exit bar, the matched trade's own round-trip cost "
    "deducted"
)

#: Trades the intraday baseline scores at most. Each is paired with `draws` random entries on
#: its own symbol's 5-minute frame, so the cost is symbol frames loaded, not trades. A
#: seeded sample of 2000 gives a standard error on the mean excess well under 1 bps on the
#: ideas recorded so far.
INTRADAY_MAX_TRADES = 2000


def _bar_times(index: Any) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.values.astype("datetime64[ns]").astype(np.int64)


def _as_ns(value: Any) -> int:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return int(ts.to_datetime64().astype("datetime64[ns]").astype(np.int64))


def sample_trades(trades: Sequence[Any], max_trades: int, seed: int = 0) -> list[Any]:
    """A seeded sample of at most `max_trades` trades, in the same sorted order every time."""
    ordered = sorted(trades, key=lambda t: (t.symbol, str(t.entry_time), str(t.exit_time)))
    if len(ordered) <= max_trades:
        return ordered
    rng = np.random.default_rng(seed)
    keep = np.sort(rng.choice(len(ordered), size=max_trades, replace=False))
    return [ordered[i] for i in keep]


def random_entry_baseline_intraday(
    trades: Sequence[Any],
    frames: Mapping[str, pd.DataFrame],
    *,
    draws: int = DRAWS_PER_TRADE,
    seed: int = 0,
    max_trades: int = INTRADAY_MAX_TRADES,
    population_trades: int | None = None,
) -> dict[str, Any] | None:
    """The matched random-entry result for intraday `trades`, or None when none can be matched.

    Same definition as `random_entry_baseline` with two changes for bars inside a session:
    the hold is counted in intraday bars, and a random entry must start and end in the SAME
    session, so an overnight gap is never scored as a hold. A seeded sample of at most
    `max_trades` trades is scored and the record says so (`sampled`, `population_trades`),
    because loading every symbol's 5-minute history is what made this baseline unaffordable.
    `frames` maps symbol to a 5-minute frame with `Open`, `Close` and a `DatetimeIndex`.

    A caller that already sampled with `sample_trades` (to load only the sampled trades'
    symbols) passes the full trade count as `population_trades`, so the record still reports
    that it is a sample.
    """
    population = population_trades if population_trades is not None else len(trades)
    ordered = sorted(trades, key=lambda t: (t.symbol, str(t.entry_time), str(t.exit_time)))
    rng = np.random.default_rng(seed)
    if len(ordered) > max_trades:
        ordered = sample_trades(ordered, max_trades, seed)
    sampled = population > len(ordered)

    prepared: dict[str, tuple] = {}
    starts_cache: dict[tuple[str, int], np.ndarray] = {}
    matched: list[tuple[Any, int]] = []
    skipped = 0
    for trade in ordered:
        frame = frames.get(trade.symbol)
        if frame is None or trade.entry_time is None or trade.exit_time is None:
            skipped += 1
            continue
        if trade.symbol not in prepared:
            prepared[trade.symbol] = (
                frame["Open"].to_numpy(dtype=float),
                frame["Close"].to_numpy(dtype=float),
                _bar_times(frame.index),
                _day_numbers(frame.index),
            )
        _, _, times, days = prepared[trade.symbol]
        start = int(np.searchsorted(times, _as_ns(trade.entry_time), side="left"))
        end = int(np.searchsorted(times, _as_ns(trade.exit_time), side="left"))
        hold = end - start
        if hold < 0:
            skipped += 1
            continue
        key = (trade.symbol, hold)
        if key not in starts_cache:
            n = len(days)
            starts_cache[key] = (
                np.flatnonzero(days[: n - hold] == days[hold:])
                if hold < n
                else np.array([], dtype=int)
            )
        if len(starts_cache[key]) == 0:
            skipped += 1
            continue
        matched.append((trade, hold))
    if not matched:
        return None

    base_gross = np.empty(len(matched))
    trade_net = np.empty(len(matched))
    trade_cost = np.empty(len(matched))
    for i, (trade, hold) in enumerate(matched):
        opens, closes, _, _ = prepared[trade.symbol]
        pool = starts_cache[(trade.symbol, hold)]
        entries = pool[rng.integers(0, len(pool), size=draws)]
        moves = (closes[entries + hold] - opens[entries]) / opens[entries] * 1e4
        base_gross[i] = trade.direction * moves.mean()
        trade_net[i] = trade.net_bps
        trade_cost[i] = trade.gross_bps - trade.net_bps
    base_net = base_gross - trade_cost
    diff = trade_net - base_net

    out: dict[str, Any] = {
        "method": METHOD_INTRADAY,
        "draws_per_trade": draws,
        "seed": seed,
        "trades": len(matched),
        "skipped": skipped,
        "sampled": bool(sampled),
        "population_trades": population,
    }
    _put(out, "gross_bps", base_gross.mean())
    _put(out, "net_bps", base_net.mean())
    _put(out, "trade_net_bps", trade_net.mean())
    _put(out, "excess_net_bps", diff.mean())
    trade_days = np.array([str(m[0].day) for m in matched])
    _put(out, "excess_t_stat", clustered_t(diff, trade_days), 3)
    return out
