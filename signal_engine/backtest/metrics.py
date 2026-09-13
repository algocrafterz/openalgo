"""Trade-list statistics.

Four conventions decide whether a comparison means anything:

  BASIS POINTS, NOT ONLY R.  R is not comparable across configurations that use
  different stop widths. Widening the stop shrinks both the gross R and the cost in R,
  so an R-only table silently rewards wide stops. `gross_bps` is expectancy in basis
  points of notional, which is directly comparable with the cost line.

  A t-STATISTIC THAT ACCOUNTS FOR THE SAME DAY.  Trades taken across a basket on one
  session are not independent draws - they share that session's market move. Treating
  200 names on a trend day as 200 observations overstates the evidence. `t` here is
  computed on DAILY means, which is the standard cluster-robust treatment and on this
  repo's own ORB run shrank |t| from 7.83 to 5.82. The naive figure is kept as
  `t_naive` so the gap is visible rather than argued about.

  A DRAWDOWN IN CALENDAR ORDER.  The harness collects trades symbol by symbol, so the
  raw list runs A's whole history, then B's. A cumulative sum over that order describes
  an equity curve nobody could have traded. Trades are sorted by exit time first.

  A HURDLE THAT KNOWS HOW MANY THINGS WERE TRIED.  Ten strategies tested at |t| > 2
  will hand you one winner from noise about 40% of the time. `hurdle_t()` gives the
  threshold that holds the false-positive rate at 5% across the whole search.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats as _st

#: NSE sessions in a year, for annualising.
TRADING_DAYS = 252

_EMPTY = {"n": 0, "win": np.nan, "gross_bps": np.nan, "net_R": np.nan, "t": np.nan,
          "t_naive": np.nan, "total_R": 0.0, "payoff": np.nan, "stop_pct": np.nan,
          "max_dd_R": 0.0, "days": 0, "R_per_day": np.nan, "sharpe": np.nan}


def _chronological(trades):
    """Trades in the order they actually closed. See the drawdown note above."""
    return sorted(trades, key=lambda t: (t.exit_time is None, t.exit_time))


def clustered_t(r: np.ndarray, day: np.ndarray) -> float:
    """t-statistic on daily means - one observation per session, not per trade.

    The cheapest honest correction for cross-sectional correlation. A basket strategy
    long 40 names into the same afternoon rally has taken one bet; this counts it once.
    """
    if len(r) < 3:
        return np.nan
    daily = pd.Series(r).groupby(pd.Series(day).to_numpy()).mean()
    if len(daily) < 3 or daily.std(ddof=1) == 0:
        return np.nan
    return float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily))))


def hurdle_t(n_trials: int, alpha: float = 0.05, dof: int = 200) -> float:
    """The |t| a result must clear when `n_trials` configurations were searched.

    Sidak, not Bonferroni: exact for independent trials and slightly less punishing.
    Count every variant actually looked at - each strategy, each sweep value, each
    ablation toggle - not the number finally reported. The gap matters: at one trial
    the bar is 1.97, at fifty it is 3.5.
    """
    n_trials = max(1, int(n_trials))
    per = 1.0 - (1.0 - alpha) ** (1.0 / n_trials)
    return float(abs(_st.t.ppf(per / 2.0, dof)))


def summary(trades, label: str = "") -> dict:
    if not trades:
        return {"label": label, **_EMPTY}
    trades = _chronological(trades)
    r = np.array([t.r_net for t in trades])
    g = np.array([t.r_gross for t in trades])
    rp = np.array([t.risk_pct for t in trades])
    day = np.array([t.day for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    eq = np.cumsum(r)
    naive = (r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
             if len(r) > 2 and r.std() > 0 else np.nan)

    # Portfolio view: one number per session, so a Sharpe means what it usually means.
    daily = pd.Series(r).groupby(day).sum()
    n_days = int(daily.size)
    sharpe = (float(daily.mean() / daily.std(ddof=1)) * np.sqrt(TRADING_DAYS)
              if n_days > 2 and daily.std(ddof=1) > 0 else np.nan)

    return {
        "label": label,
        "n": len(r),
        "win": round(100 * float((r > 0).mean()), 1),
        "gross_bps": round(float((g * rp).mean() * 100), 2),
        "net_R": round(float(r.mean()), 3),
        "t": round(clustered_t(r, day), 2),
        "t_naive": round(float(naive), 2) if np.isfinite(naive) else np.nan,
        "total_R": round(float(r.sum()), 1),
        "payoff": round(float(wins.mean() / abs(losses.mean())), 2)
        if len(wins) and len(losses) else np.nan,
        "stop_pct": round(float(np.median(rp)), 3),
        "max_dd_R": round(float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0, 1),
        "days": n_days,
        "R_per_day": round(float(daily.mean()), 3) if n_days else np.nan,
        "sharpe": round(sharpe, 2) if np.isfinite(sharpe) else np.nan,
    }


def by_symbol(trades) -> pd.DataFrame:
    """Dispersion across the basket. A result carried by two symbols is noise."""
    cols = ["symbol", "n", "total_R", "net_R"]
    if not trades:
        # A strategy that never fires a single signal is itself a finding (its
        # entry condition may simply be too narrow to trigger over this window) -
        # this must return the empty-but-correctly-shaped frame the caller expects,
        # not crash trying to sort a column that was never created.
        return pd.DataFrame(columns=cols)
    rows: dict[str, list[float]] = {}
    for t in trades:
        rows.setdefault(t.symbol, []).append(t.r_net)
    return pd.DataFrame([
        {"symbol": s.replace(".NS", ""), "n": len(v),
         "total_R": round(float(np.sum(v)), 1), "net_R": round(float(np.mean(v)), 3)}
        for s, v in rows.items()], columns=cols).sort_values("total_R", ascending=False)


def by_reason(trades) -> pd.DataFrame:
    """Exit mix. Tells you whether the strategy is actually reaching its target."""
    df = pd.DataFrame([{"reason": t.reason, "r": t.r_net} for t in trades])
    if df.empty:
        return df
    g = df.groupby("reason").agg(n=("r", "size"), net_R=("r", "mean"), total_R=("r", "sum")).round(3)
    g["pct"] = (100 * g["n"] / len(df)).round(1)
    return g.sort_values("n", ascending=False)


def by_month(trades) -> pd.DataFrame:
    """Is the result spread over the sample, or is it one good month?

    The single most useful robustness plot for a short sample, and the one that most
    often kills a strategy that looked fine in aggregate.
    """
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame([{"m": pd.Timestamp(t.day).to_period("M"), "r": t.r_net} for t in trades])
    g = df.groupby("m").agg(n=("r", "size"), total_R=("r", "sum"), net_R=("r", "mean")).round(3)
    g["cum_R"] = g["total_R"].cumsum().round(1)
    return g


def table(rows) -> str:
    return pd.DataFrame(rows).to_string(index=False)


def trades_frame(trades) -> pd.DataFrame:
    """Full trade log, for inspecting individual trades or exporting."""
    return pd.DataFrame([{
        "symbol": t.symbol, "day": t.day, "dir": "LONG" if t.direction == 1 else "SHORT",
        "tag": t.tag, "entry_time": t.entry_time, "entry": t.entry, "sl": t.sl,
        "tp": t.tp, "exit_time": t.exit_time, "exit": t.exit, "reason": t.reason,
        "risk_pct": round(t.risk_pct, 3), "r_gross": round(t.r_gross, 3),
        "r_net": round(t.r_net, 3)} for t in _chronological(trades)])
