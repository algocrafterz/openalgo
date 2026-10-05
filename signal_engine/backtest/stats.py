"""Statistics the acceptance criteria name but `metrics.py` does not compute.

  Newey-West t          G2.6  daily P&L with an autocorrelation adjustment
  Deflated Sharpe       G2.3  Bailey and Lopez de Prado (2014)
  Min track record      G2.7  Bailey and Lopez de Prado (2012)
  PBO by CSCV           G2.4  Bailey, Borwein, Lopez de Prado and Zhu (2016)
  Walk-forward windows  G3.3  rolling, fixed-parameter windows

All Sharpe figures here are PER PERIOD (per session when fed daily P&L), never annualised,
because the deflation formulas are written for the sampling distribution of the per-period
ratio. Annualise only for display.
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats as _st

#: Euler-Mascheroni constant, used by the expected-maximum-Sharpe approximation.
EULER_GAMMA = 0.5772156649015329

#: The smallest daily sample the Sharpe-family functions will score.
MIN_OBS = 30


def newey_west_t(x: np.ndarray, lags: int | None = None) -> float:
    """t-statistic of the mean with a Bartlett-weighted (Newey-West) long-run variance.

    `lags` defaults to floor(4 * (n / 100) ** (2 / 9)), the usual rule of thumb. With
    `lags=0` this is the plain t. Returns NaN for fewer than 3 points or a constant series.
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3 or np.ptp(x) == 0:
        return float("nan")
    if lags is None:
        lags = int(math.floor(4 * (n / 100) ** (2 / 9)))
    lags = max(0, min(lags, n - 1))
    e = x - x.mean()
    lrv = float(e @ e) / n
    for k in range(1, lags + 1):
        weight = 1.0 - k / (lags + 1)
        lrv += 2.0 * weight * float(e[k:] @ e[:-k]) / n
    if lrv <= 0:
        return float("nan")
    return float(x.mean() / math.sqrt(lrv / n))


def _moments(x: np.ndarray) -> tuple[float, float, float, int]:
    """Per-period Sharpe, skewness, raw kurtosis (3 for a normal) and sample size."""
    n = len(x)
    sd = float(x.std(ddof=1))
    if not sd > 1e-12 * max(1.0, abs(float(x.mean()))):
        return float("nan"), float("nan"), float("nan"), n
    sharpe = float(x.mean()) / sd
    return sharpe, float(_st.skew(x)), float(_st.kurtosis(x, fisher=False)), n


def _sharpe_variance_term(sharpe: float, skew: float, kurt: float) -> float:
    return 1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe**2


def probabilistic_sharpe(x: np.ndarray, benchmark: float = 0.0) -> float:
    """Probability that the true per-period Sharpe exceeds `benchmark`, allowing for skew
    and fat tails. NaN when the series is too short or constant."""
    x = np.asarray(x, dtype=float)
    if len(x) < MIN_OBS:
        return float("nan")
    sharpe, skew, kurt, n = _moments(x)
    if not math.isfinite(sharpe):
        return float("nan")
    term = _sharpe_variance_term(sharpe, skew, kurt)
    if term <= 0:
        return float("nan")
    return float(_st.norm.cdf((sharpe - benchmark) * math.sqrt(n - 1) / math.sqrt(term)))


def expected_max_sharpe(n_trials: int, sr_var: float) -> float:
    """Expected best per-period Sharpe among `n_trials` zero-edge strategies whose Sharpe
    ratios have variance `sr_var`. Zero for a single trial, which has nothing to deflate."""
    n = int(n_trials)
    if n <= 1:
        return 0.0
    z1 = _st.norm.ppf(1.0 - 1.0 / n)
    z2 = _st.norm.ppf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(sr_var) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(
    x: np.ndarray, n_trials: int, sr_var: float | None = None
) -> dict[str, Any] | None:
    """Deflated Sharpe ratio of a per-session P&L series after `n_trials` configurations.

    `sr_var` is the variance of the per-period Sharpe across the trials tried. The run
    record stores only the trial COUNT, so by default it is taken as the sampling variance
    of one Sharpe under no edge, 1 / (n - 1). That is an assumption, not a measurement; a
    caller that kept every trial's Sharpe should pass the real variance.
    Returns None when the series is too short or constant.
    """
    x = np.asarray(x, dtype=float)
    if len(x) < MIN_OBS:
        return None
    sharpe, _, _, n = _moments(x)
    if not math.isfinite(sharpe):
        return None
    variance = sr_var if sr_var is not None else 1.0 / (n - 1)
    threshold = expected_max_sharpe(n_trials, variance)
    dsr = probabilistic_sharpe(x, benchmark=threshold)
    if not math.isfinite(dsr):
        return None
    return {
        "dsr": round(dsr, 4),
        "sharpe_daily": round(sharpe, 4),
        "sharpe_threshold": round(threshold, 4),
        "n_obs": n,
        "n_trials": int(n_trials),
        "sr_var_assumed": sr_var is None,
    }


def min_track_record_length(x: np.ndarray, benchmark: float = 0.0, alpha: float = 0.05) -> float:
    """Sessions needed before the observed Sharpe beats `benchmark` at confidence 1 - alpha.

    Infinite when the observed Sharpe does not exceed the benchmark; NaN when undefined.
    """
    x = np.asarray(x, dtype=float)
    if len(x) < MIN_OBS:
        return float("nan")
    sharpe, skew, kurt, _ = _moments(x)
    if not math.isfinite(sharpe):
        return float("nan")
    if sharpe <= benchmark:
        return float("inf")
    z = _st.norm.ppf(1.0 - alpha)
    term = _sharpe_variance_term(sharpe, skew, kurt)
    return float(1.0 + term * (z / (sharpe - benchmark)) ** 2)


def pbo_cscv(perf: np.ndarray, n_slices: int = 16) -> float:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation.

    `perf` is a (periods x strategies) matrix of P&L for every variant tried, on the same
    periods. Split the periods into `n_slices` blocks, take every way to pick half as the
    training set, pick the best variant in training, and record where it ranks on the
    held-out half. PBO is the share of splits where the training winner lands at or below
    the median out of sample. Near 0.5 means selection is no better than chance.
    """
    perf = np.asarray(perf, dtype=float)
    if perf.ndim != 2 or perf.shape[1] < 2:
        raise ValueError("pbo_cscv needs a (periods x strategies) matrix with >= 2 strategies")
    if n_slices < 2 or n_slices % 2:
        raise ValueError("n_slices must be an even number >= 2")
    n_periods, n_strategies = perf.shape
    if n_periods < n_slices:
        raise ValueError(f"need at least {n_slices} periods, got {n_periods}")
    blocks = np.array_split(np.arange(n_periods), n_slices)
    block_sum = np.array([perf[b].sum(axis=0) for b in blocks])
    block_n = np.array([len(b) for b in blocks], dtype=float)
    below = 0
    total = 0
    for train in combinations(range(n_slices), n_slices // 2):
        test = [i for i in range(n_slices) if i not in train]
        is_mean = block_sum[list(train)].sum(axis=0) / block_n[list(train)].sum()
        oos_mean = block_sum[test].sum(axis=0) / block_n[test].sum()
        best = int(np.argmax(is_mean))
        rank = float((oos_mean < oos_mean[best]).sum() + 1) / (n_strategies + 1)
        logit = math.log(rank / (1.0 - rank))
        below += logit <= 0
        total += 1
    return below / total


def walk_forward_windows(
    start: Any, end: Any, months: int = 6
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Consecutive, non-overlapping windows of `months` months; a short trailing one is dropped.

    Each window is [from, to) so a trade on a boundary belongs to exactly one.
    """
    cursor, stop = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    while True:
        nxt = cursor + pd.DateOffset(months=months)
        if nxt > stop + pd.Timedelta(days=1):
            break
        out.append((cursor, nxt))
        cursor = nxt
    return out


def walk_forward(
    trades, months: int = 6, start: Any = None, end: Any = None, values: Any = None
) -> dict[str, Any]:
    """Net result per rolling window with the strategy's parameters held fixed.

    This is a stability check across successive windows, not a re-fit per window: the
    shipped defaults are what every window scores. A window with no trades counts as not
    positive, so a strategy cannot pass by being silent for years.

    `values` is each trade's net result in the caller's unit, in trade order; it defaults to
    `r_net`. A trade without a stop has no R (`r_net` is NaN), so a caller scoring such trades
    passes another unit, such as net bps.
    """
    trades = list(trades)
    if not trades:
        return {"windows": 0, "positive": 0, "empty": 0, "share_positive": float("nan"), "rows": []}
    days = pd.DatetimeIndex([pd.Timestamp(t.day).normalize() for t in trades])
    if values is None:
        r = np.array([t.r_net for t in trades], dtype=float)
    else:
        r = np.asarray(values, dtype=float)
        if r.shape != (len(trades),):
            raise ValueError(f"values has {r.size} entries for {len(trades)} trades")
    windows = walk_forward_windows(start or days.min(), end or days.max(), months)
    rows = []
    for lo, hi in windows:
        pick = (days >= lo) & (days < hi)
        n = int(pick.sum())
        mean = float(r[pick].mean()) if n else float("nan")
        rows.append(
            {
                "from": lo.date().isoformat(),
                "to": hi.date().isoformat(),
                "trades": n,
                "net_r": None if not n else round(mean, 4),
            }
        )
    positive = sum(1 for row in rows if row["net_r"] is not None and row["net_r"] > 0)
    empty = sum(1 for row in rows if row["trades"] == 0)
    return {
        "windows": len(rows),
        "positive": positive,
        "empty": empty,
        "share_positive": positive / len(rows) if rows else float("nan"),
        "rows": rows,
    }
