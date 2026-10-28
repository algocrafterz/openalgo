"""Freeze the backtest reference bands the v3 paper verdict is judged against (run once).

    PYTHONPATH=. uv run python signal_engine/research/ideas/idea-034-multi-day-balance-breakout/paper/build_reference.py

Input: the limit-order study's per-signal replay (../limit-order-study/results/sample_a.csv), the
v2 signals (H at least 1.5 percent of the edge) under the L1 15m rule, unfilled counted as 0.
For each signal count n on a grid, 20,000 bootstrap paths of n signals drawn with replacement
give the 5th/95th percentile of the per-signal mean and the 95th/99th percentile of the maximum
drawdown of the cumulative per-signal net (bps of the Rs 1,00,000 notional). Writes
reference.json beside this script. Seeded, so a rerun gives the same numbers.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SAMPLE = HERE.parent / "limit-order-study" / "results" / "sample_a.csv"
GRID = list(range(25, 425, 25))
PATHS = 20_000
SEED = 34


def per_signal(d: pd.DataFrame) -> np.ndarray:
    v2 = d[d.h_pct >= 1.5]
    filled = v2["L1w15|filled"].astype(bool)
    return v2["L1w15|net"].where(filled, 0.0).to_numpy(float), filled.to_numpy()


def max_drawdown(paths: np.ndarray) -> np.ndarray:
    cum = np.cumsum(paths, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.zeros((len(cum), 1)), cum], axis=1), axis=1)[
        :, 1:
    ]
    return (peak - cum).max(axis=1)


def main() -> None:
    x, filled = per_signal(pd.read_csv(SAMPLE))
    rng = np.random.default_rng(SEED)
    bands = {}
    for n in GRID:
        paths = x[rng.integers(0, len(x), size=(PATHS, n))]
        means = paths.mean(axis=1)
        dd = max_drawdown(paths)
        bands[str(n)] = {
            "mean_p5": round(float(np.percentile(means, 5)), 3),
            "mean_p95": round(float(np.percentile(means, 95)), 3),
            "dd_p95": round(float(np.percentile(dd, 95)), 1),
            "dd_p99": round(float(np.percentile(dd, 99)), 1),
        }
    out = {
        "source": "limit-order-study/results/sample_a.csv, v2 (h_pct >= 1.5), L1w15, unfilled = 0",
        "signals": int(len(x)),
        "fill_rate": round(float(filled.mean()), 4),
        "net_per_signal_bps": round(float(x.mean()), 3),
        "net_per_fill_bps": round(float(x[filled].mean()), 3),
        "bootstrap": {"paths": PATHS, "seed": SEED, "unit": "bps of the Rs 1,00,000 notional"},
        "bands": bands,
    }
    (HERE / "reference.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "bands"}, indent=2))
    print({n: bands[str(n)] for n in (50, 100, 200)})


if __name__ == "__main__":
    main()
