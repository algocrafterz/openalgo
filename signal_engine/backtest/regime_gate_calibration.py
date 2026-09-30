"""Calibrate the X.2 regime-dispersion heuristic against a known-null and a known-edge book.

X.2 asks whether a strategy's edge is spread across market regimes or sits in one. The
threshold ("net positive in at least k of the n sampled regimes of each dimension") is a
heuristic, not a number from the acceptance criteria, so it is measured rather than
assumed.

METHOD.  Take a real trade list with its entry-day regime labels, then:

  1. Demean R WITHIN each regime, so every regime has a true mean of zero. This is the
     null: a strategy with no edge anywhere. Adding `edge` R to every trade turns it into
     a strategy with a uniform edge of that size.
  2. Resample SESSIONS with replacement (a day-block bootstrap), keeping each day's trades
     and labels together. Trades on one day share that day's market move, so resampling
     trades one by one would make the regime means look far less noisy than they are.
  3. For each resample compute each regime's mean R, drop regimes with fewer than
     `min_trades`, and ask whether a rule passes.

The output is the rate at which each rule passes: under the null it is the false-pass
rate, under a uniform edge it is the power. The threshold is the loosest rule whose null
false-pass rate is under a stated target, which keeps the most power for a given false-pass
budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from signal_engine.backtest.regime import DIMENSION_LABELS, MIN_REGIME_TRADES

#: Null false-pass rate the chosen rule must stay under. One in five is loose enough to
#: keep power and strict enough that a zero-edge book usually fails.
TARGET_FALSE_PASS = 0.20


@dataclass(frozen=True)
class Rule:
    """`minimum` positive regimes per judged dimension; None means every judged regime."""

    name: str
    minimum: int | None

    def passes(self, positive: int, evaluated: int) -> bool:
        need = evaluated if self.minimum is None else min(self.minimum, evaluated)
        return positive >= need


RULES = (Rule("k1", 1), Rule("k2", 2), Rule("k3", 3), Rule("all", None))


def day_regime_matrices(
    r: np.ndarray, day: np.ndarray, labels: np.ndarray, order: tuple[str, ...]
) -> tuple[np.ndarray, np.ndarray]:
    """R summed and trades counted per (day index, regime). Unlabelled trades are dropped."""
    n_days = int(day.max()) + 1 if len(day) else 0
    sums = np.zeros((n_days, len(order)))
    cnts = np.zeros((n_days, len(order)), dtype=np.int64)
    column = {name: i for i, name in enumerate(order)}
    for value, d, label in zip(r, day, labels, strict=True):
        j = column.get(label) if label is not None else None
        if j is None:
            continue
        sums[d, j] += value
        cnts[d, j] += 1
    return sums, cnts


def pass_rates(
    r: np.ndarray,
    day: np.ndarray,
    labels: Mapping[str, np.ndarray],
    *,
    edge: float,
    n_boot: int = 2000,
    seed: int = 0,
    min_trades: int = MIN_REGIME_TRADES,
    rules: tuple[Rule, ...] = RULES,
) -> dict[str, dict[str, float]]:
    """Share of day-block resamples each rule passes, with every regime's true mean = `edge`."""
    per_dim = {}
    for dim, order in DIMENSION_LABELS.items():
        sums, cnts = day_regime_matrices(r, day, np.asarray(labels[dim], dtype=object), order)
        totals = cnts.sum(axis=0)
        means = np.divide(sums.sum(axis=0), totals, out=np.zeros(len(order)), where=totals > 0)
        per_dim[dim] = (sums - means * cnts + edge * cnts, cnts)

    n_days = next(iter(per_dim.values()))[1].shape[0]
    rng = np.random.default_rng(seed)
    weights = rng.multinomial(n_days, np.full(n_days, 1.0 / n_days), size=n_boot).astype(float)

    evaluated = {}
    positive = {}
    for dim, (sums, cnts) in per_dim.items():
        s, c = weights @ sums, weights @ cnts
        ok = c >= min_trades
        mean = np.divide(s, c, out=np.zeros_like(s), where=c > 0)
        evaluated[dim] = ok.sum(axis=1)
        positive[dim] = ((mean > 0) & ok).sum(axis=1)

    judged_any = np.zeros(n_boot, dtype=bool)
    for dim in per_dim:
        judged_any |= evaluated[dim] >= 2
    out: dict[str, dict[str, float]] = {}
    for rule in rules:
        passed = judged_any.copy()
        for dim in per_dim:
            judged = evaluated[dim] >= 2
            need = (
                evaluated[dim] if rule.minimum is None else np.minimum(rule.minimum, evaluated[dim])
            )
            passed &= ~judged | (positive[dim] >= need)
        out[rule.name] = {
            "pass": round(float(passed.mean()), 4),
            "judged": round(float(judged_any.mean()), 4),
        }
    return out


def choose_rule(
    null_rates: Mapping[str, Mapping[str, float]], target: float = TARGET_FALSE_PASS
) -> str:
    """The loosest rule (first in `RULES` order) whose null pass rate is within `target`."""
    for rule in RULES:
        entry = null_rates.get(rule.name)
        if entry is not None and entry["pass"] <= target:
            return rule.name
    raise ValueError(f"no rule keeps the null false-pass rate within {target:.0%}")


def _book_arrays(trades, regime_days):
    """R, day index and per-dimension labels for a trade list, each placed by its day."""
    import pandas as pd

    days = sorted({pd.Timestamp(t.day).normalize() for t in trades})
    index = {d: i for i, d in enumerate(days)}
    r = np.array([t.r_net for t in trades], dtype=float)
    day = np.array([index[pd.Timestamp(t.day).normalize()] for t in trades])
    keys = pd.DatetimeIndex([pd.Timestamp(t.day).normalize() for t in trades])
    labels = {}
    for dim in DIMENSION_LABELS:
        lookup = regime_days[dim].reindex(keys)
        labels[dim] = np.array(
            [None if pd.isna(v) else str(v) for v in lookup.tolist()], dtype=object
        )
    return r, day, labels


def main(argv: list[str] | None = None) -> None:
    """Run the calibration on real swing trade lists and print a table.

    uv run --group analysis python -m signal_engine.backtest.regime_gate_calibration
    """
    import argparse
    import json

    from signal_engine.backtest import regime, regime_data
    from signal_engine.backtest.record_run import execute_backtest

    parser = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    parser.add_argument("--keys", nargs="+", default=["gap_rsi", "ibs_meanrev", "ema_pullback"])
    parser.add_argument("--edges", nargs="+", type=float, default=[0.0, 0.05, 0.10, 0.20])
    parser.add_argument("--boot", type=int, default=3000)
    parser.add_argument("--out", help="write the raw result table as JSON")
    args = parser.parse_args(argv)

    inputs = regime_data.load_regime_inputs()
    regime_days = regime.build_regime_days(inputs.close, inputs.vix)
    result = {}
    for key in args.keys:
        trades = execute_backtest(key).trades
        r, day, labels = _book_arrays(trades, regime_days)
        result[key] = {"trades": len(r), "observed_mean_r": round(float(r.mean()), 4), "edges": {}}
        for edge in args.edges:
            result[key]["edges"][str(edge)] = pass_rates(
                r, day, labels, edge=edge, n_boot=args.boot, seed=0
            )
    print(json.dumps(result, indent=1))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(result, fh, indent=1)


if __name__ == "__main__":
    main()
