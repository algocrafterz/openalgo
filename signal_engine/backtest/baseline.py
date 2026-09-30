"""Random-entry baseline: what the same names earn when entered on arbitrary days.

A long-only strategy tested through a rising market earns the market. Dividend growth
showed +380 bps a trade and was still rejected because random entry with the same holding
period earned more. This puts that comparison in every run record so a result that is only
beta cannot pass for skill.

Definition, fixed here so runs stay comparable:

  MATCHED PER TRADE.  Each trade is paired with random entries on the SAME symbol, held for
  the SAME number of bars, in the SAME direction, with the same round-trip cost deducted.
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
    "open to close, round-trip cost deducted"
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
    round_trip_bps: float,
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
    for i, (trade, hold, opens, closes) in enumerate(matched):
        entries = rng.integers(0, len(closes) - hold, size=draws)
        moves = (closes[entries + hold] - opens[entries]) / opens[entries] * 1e4
        base_gross[i] = trade.direction * moves.mean()
        trade_net[i] = trade.r_net * trade.risk_pct * 100
    base_net = base_gross - round_trip_bps
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
