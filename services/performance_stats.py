"""
Central definitions for the Strategy Performance page's statistics.

Pure computation: no database, broker, clock or logging, so every formula can be
checked against an independent reference (see test/test_performance_stats.py).

Daily series
------------
One observation per NSE trading session in the range, including sessions with
no trade (0 P&L). Weekends and exchange holidays are not observations. The
caller supplies the session list; `weekday_sessions` is the fallback.
A session's P&L is the NET P&L (realized minus estimated charges) of every
trade closed that day, across all selected strategies, so overlapping positions
are one portfolio observation.

Daily return = daily net P&L / assumed allocated capital. The capital is the
configured amount per strategy times the number of selected strategies. It is
an assumption, not measured deployed capital.

Sharpe (annualised)
    (mean(r) - rf_daily) / std(r, ddof=1) * sqrt(252)
    rf_daily = annual risk-free rate / 252 (default 0).

Sortino (annualised)
    (mean(r) - target) / DD * sqrt(252)
    DD = sqrt(mean(min(r - target, 0) ** 2)), the mean taken over ALL sessions.
    target = minimum acceptable daily return (default 0).

With a constant capital denominator, zero risk-free rate and zero target, both
ratios are unchanged when capital changes (mean and deviation scale together).
A non-zero risk-free rate or target breaks that on purpose.

Both ratios are withheld (None, with a warning) when the range has fewer than
MIN_SESSIONS sessions; Sortino also needs MIN_DOWNSIDE_SESSIONS sessions below
the target. A zero or flat deviation returns None, never infinity.

Edge check
----------
Expectancy is the mean NET P&L per trade. Intervals: Student-t at 90% and 95%,
and a day-clustered bootstrap (whole trading days resampled, so trades that
share a session move together) with BOOTSTRAP_RESAMPLES draws and a fixed seed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import date, timedelta

import numpy as np

TRADING_DAYS_PER_YEAR = 252

MIN_SESSIONS = 60
MIN_DOWNSIDE_SESSIONS = 10

BOOTSTRAP_RESAMPLES = 5000
BOOTSTRAP_SEED = 20260925

EDGE_MIN_TRADES = 30
EDGE_MIN_DAYS = 10

VERDICT_INSUFFICIENT = "insufficient_sample"
VERDICT_SUPPORTED = "positive_supported"
VERDICT_POSITIVE_INCONCLUSIVE = "positive_inconclusive"
VERDICT_NEGATIVE = "negative"
VERDICT_INCONCLUSIVE = "inconclusive"


# ---------------------------------------------------------------- sessions


def weekday_sessions(start_date: str, end_date: str) -> list[str]:
    """Monday to Friday dates in [start_date, end_date] (ISO strings)."""
    day, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
    out = []
    while day <= last:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def trading_sessions(
    start_date: str,
    end_date: str,
    traded_dates: Iterable[str] = (),
    is_holiday: Callable[[date], bool] | None = None,
) -> list[str]:
    """Sorted session dates: weekdays that are not holidays, plus any date a
    trade closed on (a special session on a weekend still counts).

    `is_holiday` is injected so this module stays free of I/O.
    """
    sessions = set()
    for iso in weekday_sessions(start_date, end_date):
        if is_holiday is None or not is_holiday(date.fromisoformat(iso)):
            sessions.add(iso)
    sessions.update(d for d in traded_dates if start_date <= d <= end_date)
    return sorted(sessions)


def daily_pnl_values(pnl_by_date: dict[str, float], sessions: Iterable[str]) -> np.ndarray:
    """P&L per session, 0.0 for a session with no trade, in session order."""
    return np.array([float(pnl_by_date.get(s, 0.0)) for s in sessions], dtype=float)


def daily_returns(daily_pnl: np.ndarray, capital: float) -> np.ndarray:
    """Daily P&L over a constant capital. Raises ValueError for capital <= 0."""
    if not capital or capital <= 0 or not math.isfinite(capital):
        raise ValueError("capital must be a positive number")
    return np.asarray(daily_pnl, dtype=float) / float(capital)


# ---------------------------------------------------------------- ratios


def _is_flat(arr: np.ndarray) -> bool:
    scale = float(np.abs(arr).max()) if arr.size else 0.0
    return float(np.ptp(arr)) <= 1e-12 * scale


def sharpe_ratio(returns: np.ndarray, risk_free_annual: float = 0.0) -> float | None:
    """Annualised Sharpe on daily returns; None for fewer than 2 observations or no spread."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2 or _is_flat(r):
        return None
    std = float(r.std(ddof=1))
    if std <= 0:
        return None
    excess = float(r.mean()) - risk_free_annual / TRADING_DAYS_PER_YEAR
    return excess / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def downside_deviation(returns: np.ndarray, target: float = 0.0) -> float:
    """sqrt(mean(min(r - target, 0)^2)) over every observation."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.minimum(r - target, 0.0) ** 2)))


def sortino_ratio(returns: np.ndarray, target: float = 0.0) -> float | None:
    """Annualised Sortino on daily returns; None when there is no downside to divide by."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return None
    if _is_flat(r):
        return None  # no variation: a "ratio" here would only measure the level
    dd = downside_deviation(r, target)
    scale = max(float(np.abs(r).max()), abs(target))
    if dd <= 1e-12 * scale or dd == 0.0:
        return None
    return (float(r.mean()) - target) / dd * math.sqrt(TRADING_DAYS_PER_YEAR)


def _round(value: float | None, places: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, places)


def ratio_warning(
    sessions: int,
    downside: int,
    sharpe_shown: bool,
    sortino_shown: bool,
    target_is_zero: bool = True,
) -> str | None:
    """Trader-facing sentence for withheld ratios; None when both are shown."""
    if sharpe_shown and sortino_shown:
        return None
    if sessions < MIN_SESSIONS:
        return (
            f"Sharpe and Sortino are not shown. They need at least {MIN_SESSIONS} trading "
            f"sessions to be reliable, and this range has {sessions}."
        )
    parts = []
    if not sharpe_shown:
        parts.append("Sharpe")
    if not sortino_shown:
        parts.append("Sortino")
    names = " and ".join(parts)
    if downside < MIN_DOWNSIDE_SESSIONS:
        what = "losing sessions" if target_is_zero else "sessions below the target return"
        return (
            f"Sharpe and Sortino are not shown. They need at least {MIN_DOWNSIDE_SESSIONS} "
            f"{what}, and this range has {downside}."
        )
    return f"{names} cannot be calculated because daily results do not vary enough."


def risk_adjusted_summary(
    daily_pnl: np.ndarray,
    capital: float,
    risk_free_annual: float = 0.0,
    target_daily: float = 0.0,
) -> dict:
    """Gated Sharpe and Sortino for a daily net P&L series, with the counts behind them."""
    pnl = np.asarray(daily_pnl, dtype=float)
    sessions = int(pnl.size)
    returns = daily_returns(pnl, capital)
    if not np.isfinite(returns).all():
        return {
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "ratio_sessions": sessions,
            "ratio_losing_sessions": None,
            "ratio_downside_sessions": None,
            "ratio_capital": float(capital),
            "ratio_risk_free_annual": risk_free_annual,
            "ratio_target_daily": target_daily,
            "ratio_basis": "net of estimated charges, per trading session, on assumed allocated capital",
            "ratio_warning": "Sharpe and Sortino are not shown because some daily results could not be read.",
        }
    downside = int((returns < target_daily).sum())
    # Both ratios need enough sessions AND enough sessions under the target: with only a
    # handful of losing days the downside, and so the ratio, rests on almost nothing.
    enough = sessions >= MIN_SESSIONS and downside >= MIN_DOWNSIDE_SESSIONS
    sharpe = sharpe_ratio(returns, risk_free_annual) if enough else None
    sortino = sortino_ratio(returns, target_daily) if enough else None
    return {
        "sharpe_ratio": _round(sharpe),
        "sortino_ratio": _round(sortino),
        "ratio_sessions": sessions,
        "ratio_losing_sessions": int((pnl < 0).sum()),
        "ratio_downside_sessions": downside,
        "ratio_capital": float(capital),
        "ratio_risk_free_annual": risk_free_annual,
        "ratio_target_daily": target_daily,
        "ratio_basis": "net of estimated charges, per trading session, on assumed allocated capital",
        "ratio_warning": ratio_warning(
            sessions,
            downside,
            sharpe is not None,
            sortino is not None,
            target_is_zero=target_daily == 0,
        ),
    }


# ---------------------------------------------------------------- Student t


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _reg_inc_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_cdf(x: float, df: float) -> float:
    """Student-t cumulative distribution function."""
    tail = 0.5 * _reg_inc_beta(df / 2.0, 0.5, df / (df + x * x))
    return 1.0 - tail if x > 0 else tail


def t_critical(confidence: float, df: int) -> float:
    """Two-sided Student-t critical value (e.g. 0.95 with 195 df -> about 1.972)."""
    target = 0.5 + confidence / 2.0
    low, high = 0.0, 1.0
    while t_cdf(high, df) < target:
        high *= 2.0
        if high > 1e8:
            break
    for _ in range(200):
        mid = (low + high) / 2.0
        if t_cdf(mid, df) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


# ---------------------------------------------------------------- edge check


def _t(values: np.ndarray) -> float | None:
    if len(values) < 2 or _is_flat(values):
        return None
    std = float(values.std(ddof=1))
    return None if std == 0 else float(values.mean()) / (std / float(np.sqrt(len(values))))


_BOOTSTRAP_CHUNK = 250


def clustered_bootstrap_means(
    values: np.ndarray,
    clusters: list[str] | None,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> np.ndarray:
    """Per-trade mean from `resamples` draws of whole clusters (trading days) with
    replacement. Without `clusters` each trade is its own cluster.

    Draws are made in chunks so memory stays at chunk x clusters however long the history is.
    A chunked draw from one generator is the same stream as a single large draw.
    """
    arr = np.asarray(values, dtype=float)
    if clusters is None:
        sums, counts = arr, np.ones(arr.size)
    else:
        if len(clusters) != arr.size:
            raise ValueError("clusters must have one entry per value")
        order: dict[str, int] = {}
        sums_list: list[float] = []
        counts_list: list[int] = []
        for value, cluster in zip(arr, clusters, strict=True):
            idx = order.setdefault(cluster, len(order))
            if idx == len(sums_list):
                sums_list.append(0.0)
                counts_list.append(0)
            sums_list[idx] += float(value)
            counts_list[idx] += 1
        sums, counts = np.array(sums_list), np.array(counts_list, dtype=float)
    rng = np.random.default_rng(seed)
    out = np.empty(resamples)
    for start in range(0, resamples, _BOOTSTRAP_CHUNK):
        size = min(_BOOTSTRAP_CHUNK, resamples - start)
        draw = rng.integers(0, sums.size, size=(size, sums.size))
        out[start : start + size] = sums[draw].sum(axis=1) / counts[draw].sum(axis=1)
    return out


def edge_stats(
    net_pnls: list[float],
    costed: int | None = None,
    trade_dates: list[str] | None = None,
) -> dict:
    """Is the per-trade average distinguishable from luck? Inputs are per-trade NET P&L
    values (an unknown cost counts as 0; `costed` reports how many were known).

    t_stat = mean / (sample std / sqrt(n)). expectancy_ci90 / expectancy_ci95 are
    mean +/- t_critical(n - 1) standard errors. With `trade_dates` the t-test is also
    run on per-day totals (`day_t_stat`) and the bootstrap resamples whole days;
    without them it resamples single trades.

    Verdict (insufficient_sample, positive_supported, positive_inconclusive, negative,
    inconclusive): fewer than EDGE_MIN_TRADES trades or EDGE_MIN_DAYS days is
    insufficient, and so is any non-finite input. A positive mean is supported only when
    the trade t-stat and the day t-stat (when dates are given) each pass the two-sided 95%
    Student-t critical value for their own degrees of freedom, and the lower 95% bootstrap
    bound is above zero. A mean at or below zero is negative only when the mirror
    conditions hold. A series with no variation has no t-stat and is inconclusive.
    """
    n = len(net_pnls)
    if trade_dates is not None and len(trade_dates) != n:
        raise ValueError("trade_dates must have one entry per trade")
    base = {
        "edge_trades": n,
        "edge_cost_coverage": costed if costed is not None else n,
        "net_expectancy": None,
        "net_pnl_std": None,
        "t_stat": None,
        "expectancy_ci90": None,
        "expectancy_ci95": None,
        "bootstrap_ci90": None,
        "bootstrap_ci95": None,
        "bootstrap_p_nonpositive": None,
        "bootstrap_resamples": None,
        "edge_verdict": VERDICT_INSUFFICIENT,
        "edge_days": None,
        "day_t_stat": None,
    }
    if n == 0:
        return base
    arr = np.asarray(net_pnls, dtype=float)
    if not np.isfinite(arr).all():
        return base
    mean = float(arr.mean())
    base["net_expectancy"] = round(mean, 2)
    if n < 2:
        return base
    flat = _is_flat(arr)
    std = 0.0 if flat else float(arr.std(ddof=1))
    base["net_pnl_std"] = round(std, 2)
    stderr = std / math.sqrt(n)
    for level, key in ((0.90, "expectancy_ci90"), (0.95, "expectancy_ci95")):
        crit = t_critical(level, n - 1)
        base[key] = [round(mean - crit * stderr, 2), round(mean + crit * stderr, 2)]

    boot = clustered_bootstrap_means(arr, trade_dates)
    base["bootstrap_resamples"] = int(boot.size)
    lower95, upper95 = (float(v) for v in np.percentile(boot, [2.5, 97.5]))
    base["bootstrap_ci90"] = [round(float(v), 2) for v in np.percentile(boot, [5, 95])]
    base["bootstrap_ci95"] = [round(lower95, 2), round(upper95, 2)]
    base["bootstrap_p_nonpositive"] = round(float((boot <= 0).mean()), 4)

    t_stat = None
    if std > 0:
        t_stat = mean / stderr
        base["t_stat"] = round(t_stat, 2)
    day_t = None
    days = None
    if trade_dates is not None:
        by_day: dict[str, float] = {}
        for value, day in zip(arr, trade_dates, strict=True):
            by_day[day] = by_day.get(day, 0.0) + float(value)
        days = len(by_day)
        day_t = _t(np.array(list(by_day.values())))
        base["edge_days"] = days
        base["day_t_stat"] = None if day_t is None else round(day_t, 2)
        if days < EDGE_MIN_DAYS:
            return base
    if n < EDGE_MIN_TRADES:
        return base
    if t_stat is None:
        base["edge_verdict"] = VERDICT_INCONCLUSIVE
        return base

    checks = [(t_stat, t_critical(0.95, n - 1))]
    if days is not None:
        checks.append((day_t if day_t is not None else 0.0, t_critical(0.95, days - 1)))
    if mean > 0:
        supported = all(t >= crit for t, crit in checks) and lower95 > 0
        base["edge_verdict"] = VERDICT_SUPPORTED if supported else VERDICT_POSITIVE_INCONCLUSIVE
    else:
        negative = all(t <= -crit for t, crit in checks) and upper95 < 0
        base["edge_verdict"] = VERDICT_NEGATIVE if negative else VERDICT_INCONCLUSIVE
    return base


# ======================================================================================
# P1: consistency, risk, capital efficiency, entry quality, excursions, execution,
# comparison and grading. All inputs are plain lists of dicts; nothing here reads a
# database. A "trade row" is a dict with at least:
#   strategy, trade_date (YYYY-MM-DD), closed_at, net (P&L after estimated charges),
#   and optionally net_r, opened_at, direction, entry_price, closed_quantity, data_flag.
# ======================================================================================

MIN_TRADES_FOR_GRADE = 30
MIN_SESSIONS_FOR_RETURN_GRADE = EDGE_MIN_DAYS  # trades on a few days share one market
MFE_MEANINGFUL_R = 0.5  # a favourable move of at least half the initial risk
FIRST_HOUR_END = "10:15"  # NSE cash opens 09:15; the first hour ends here
ROLLING_EXPECTANCY_WINDOW = 20
ROLLING_PF_WINDOW = 50
MIN_CORRELATION_SESSIONS = 20


def profit_factor(values: Iterable[float]) -> float | None:
    """Sum of gains over sum of losses; None when there is no loss or no trade."""
    vals = [float(v) for v in values]
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    return gains / losses if losses > 0 else None


def pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 2) if whole > 0 else None


def daily_totals(rows: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        out[r["trade_date"]] = out.get(r["trade_date"], 0.0) + float(r["net"])
    return out


def ordered_trades(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (str(r.get("closed_at") or ""), str(r["trade_date"])))


# ---------------------------------------------------------------- concentration


def concentration(rows: list[dict]) -> dict:
    """How much of the net profit rests on a few sessions or trades.

    Over trading days with a trade (days with no trade carry no P&L to concentrate).
    Shares are of total NET profit and are None unless that total is positive.
    `net_ex_best_five_days` is None with five or fewer traded days.
    """
    daily = daily_totals(rows)
    out: dict = {"traded_days": len(daily), "trades": len(rows)}
    if not rows:
        return out
    total = sum(daily.values())
    ranked = sorted(daily.values(), reverse=True)
    values = np.array(ranked)
    out.update(
        net=round(total, 2),
        best_day=round(ranked[0], 2),
        worst_day=round(ranked[-1], 2),
        mean_daily=round(float(values.mean()), 2),
        median_daily=round(float(np.median(values)), 2),
        profitable_days=int((values > 0).sum()),
        profitable_days_pct=pct(float((values > 0).sum()), len(values)),
        avg_winning_day=round(float(values[values > 0].mean()), 2) if (values > 0).any() else None,
        avg_losing_day=round(float(values[values < 0].mean()), 2) if (values < 0).any() else None,
        net_ex_best_day=round(total - ranked[0], 2),
        net_ex_best_five_days=round(total - sum(ranked[:5]), 2) if len(ranked) > 5 else None,
    )
    for k in (1, 3, 5):
        out[f"top{k}_days_share_pct"] = pct(sum(ranked[:k]), total) if len(ranked) >= k else None
    nets = sorted((float(r["net"]) for r in rows), reverse=True)
    k = max(1, math.ceil(0.1 * len(nets)))
    out["top10pct_trades"] = k
    out["top10pct_trades_share_of_net_pct"] = pct(sum(nets[:k]), total)
    best_day = max(daily, key=lambda d: daily[d])
    out["best_day_date"] = best_day
    by_strategy = {}
    names = sorted({r["strategy"] for r in rows})
    for name in names:
        mine = [r for r in rows if r["strategy"] == name]
        mine_daily = daily_totals(mine)
        mine_net = sum(mine_daily.values())
        mine_best = max(mine_daily.values())
        by_strategy[name] = {
            "trades": len(mine),
            "net": round(mine_net, 2),
            "share_of_net_pct": pct(mine_net, total),
            "best_day_net": round(mine_best, 2),
            "best_day_share_of_own_pct": pct(mine_best, mine_net),
            "net_ex_best_day": round(mine_net - mine_best, 2),
            "net_ex_portfolio_best_day": round(mine_net - mine_daily.get(best_day, 0.0), 2),
        }
    out["by_strategy"] = by_strategy
    return out


# ---------------------------------------------------------------- streaks


def losing_streaks(rows: list[dict]) -> dict:
    """Consecutive losing TRADES in closing order (a winner or a flat trade ends a streak)."""
    streaks: list[int] = []
    run = 0
    for r in ordered_trades(rows):
        if float(r["net"]) < 0:
            run += 1
        else:
            if run:
                streaks.append(run)
            run = 0
    if run:
        streaks.append(run)
    return {
        "max_consecutive_losses": max(streaks) if streaks else 0,
        "avg_losing_streak": round(sum(streaks) / len(streaks), 2) if streaks else None,
        "losing_streaks": len(streaks),
    }


# ---------------------------------------------------------------- drawdown


def _drawdown_path(values: np.ndarray) -> tuple[float, int, int, int | None]:
    """(max drawdown <= 0, peak index, trough index, recovery index or None).

    The equity curve starts at 0 before the first value, so the first value can already be
    a drawdown. Indexes refer to `values`; the start (before value 0) is index -1.
    """
    equity = np.cumsum(values)
    peak_val, peak_idx = 0.0, -1
    best = (0.0, -1, -1)
    for i, e in enumerate(equity):
        if e > peak_val:
            peak_val, peak_idx = float(e), i
        dd = float(e) - peak_val
        if dd < best[0]:
            best = (dd, peak_idx, i)
    dd, p_idx, t_idx = best
    if dd == 0.0:
        return 0.0, -1, -1, None
    peak_level = 0.0 if p_idx < 0 else float(equity[p_idx])
    recovery = next((j for j in range(t_idx + 1, len(equity)) if equity[j] >= peak_level), None)
    return dd, p_idx, t_idx, recovery


def net_drawdown(
    daily_net: np.ndarray, sessions: list[str], capital_base: float, trade_nets: list[float]
) -> dict:
    """Realized-equity drawdown on NET P&L, per session close and per closed trade.

    This is realized equity, not mark-to-market: an open position's unrealized loss is not
    in it. Percent figures are of capital_base plus the peak equity. Calmar annualises over
    the sessions and is withheld below MIN_SESSIONS.
    """
    values = np.asarray(daily_net, dtype=float)
    net = float(values.sum())
    out: dict = {
        "basis": "realized equity after estimated charges; open positions are not marked to market",
        "net": round(net, 2),
        "capital_base": capital_base,
        "return_on_capital_pct": round(100.0 * net / capital_base, 2),
    }
    dd, p_idx, t_idx, rec = _drawdown_path(values)
    out["max_drawdown"] = round(dd, 2)
    peak_equity = 0.0 if p_idx < 0 else float(np.cumsum(values)[p_idx])
    out["max_drawdown_pct"] = round(100.0 * dd / (capital_base + peak_equity), 2) if dd else 0.0
    out["recovery_factor"] = round(net / abs(dd), 2) if dd < 0 else None
    if dd < 0:
        out["drawdown_peak_date"] = sessions[p_idx] if p_idx >= 0 else None
        out["drawdown_trough_date"] = sessions[t_idx]
        end = rec if rec is not None else len(values) - 1
        out["drawdown_duration_sessions"] = end - p_idx
        out["drawdown_recovered"] = rec is not None
    if len(values) >= MIN_SESSIONS and dd < 0:
        out["calmar_ratio"] = round(net * TRADING_DAYS_PER_YEAR / (len(values) * abs(dd)), 2)
    else:
        out["calmar_ratio"] = None
    seq_dd = _drawdown_path(np.asarray(trade_nets, dtype=float))[0] if trade_nets else 0.0
    out["max_drawdown_trade_sequence"] = round(seq_dd, 2)
    out["recovery_factor_trade_sequence"] = round(net / abs(seq_dd), 2) if seq_dd < 0 else None
    return out


def max_drawdown_value(daily_net: np.ndarray) -> float:
    """Largest peak-to-trough fall (<= 0) of the cumulative series starting at 0."""
    return _drawdown_path(np.asarray(daily_net, dtype=float))[0] if len(daily_net) else 0.0


# ---------------------------------------------------------------- deployment


def _minutes(value) -> float | None:
    try:
        from datetime import datetime as _dt

        return _dt.fromisoformat(str(value).replace(" ", "T")[:19]).timestamp() / 60.0
    except (ValueError, TypeError):
        return None


def deployment(rows: list[dict]) -> dict:
    """Entry value of positions held at the same moment (not margin: leverage is not netted).

    For each traded day the peak of concurrent entry value; reports their mean and the
    overall maximum. Trades with unreliable timestamps (a data_flag) or missing prices are
    left out and counted.
    """
    events_by_day: dict[str, list[tuple[float, float]]] = {}
    skipped = 0
    for r in rows:
        opened, closed = _minutes(r.get("opened_at")), _minutes(r.get("closed_at"))
        try:
            value = float(r["entry_price"]) * float(r["closed_quantity"])
        except (KeyError, TypeError, ValueError):
            value = None
        if (
            r.get("data_flag")
            or opened is None
            or closed is None
            or value is None
            or closed < opened
        ):
            skipped += 1
            continue
        events = events_by_day.setdefault(r["trade_date"], [])
        events.append((opened, value))
        events.append((closed, -value))
    peaks = []
    for events in events_by_day.values():
        current = peak = 0.0
        # at equal times an exit is processed before an entry
        for _, delta in sorted(events, key=lambda e: (e[0], e[1])):
            current += delta
            peak = max(peak, current)
        peaks.append(peak)
    return {
        "avg_daily_peak_deployed": round(sum(peaks) / len(peaks), 2) if peaks else None,
        "max_deployed": round(max(peaks), 2) if peaks else None,
        "deployment_days": len(peaks),
        "deployment_excluded": skipped,
        "deployment_basis": "entry value of positions open together; leverage is not netted",
    }


# ---------------------------------------------------------------- rolling and monthly


def rolling_series(rows: list[dict]) -> list[dict]:
    """Rolling 20-trade net expectancy and 50-trade net profit factor, by closing order."""
    ordered = ordered_trades(rows)
    nets = [float(r["net"]) for r in ordered]
    points = []
    for i, r in enumerate(ordered):
        n = i + 1
        exp20 = (
            sum(nets[n - ROLLING_EXPECTANCY_WINDOW : n]) / ROLLING_EXPECTANCY_WINDOW
            if n >= ROLLING_EXPECTANCY_WINDOW
            else None
        )
        pf50 = profit_factor(nets[n - ROLLING_PF_WINDOW : n]) if n >= ROLLING_PF_WINDOW else None
        if exp20 is None and pf50 is None:
            continue
        points.append(
            {
                "n": n,
                "date": r["trade_date"],
                "exp20": None if exp20 is None else round(exp20, 2),
                "pf50": None if pf50 is None else round(pf50, 2),
            }
        )
    return points


def monthly_table(rows: list[dict]) -> list[dict]:
    months: dict[str, list[dict]] = {}
    for r in rows:
        months.setdefault(r["trade_date"][:7], []).append(r)
    out = []
    for month in sorted(months):
        mine = months[month]
        nets = [float(r["net"]) for r in mine]
        daily = daily_totals(mine)
        pf = profit_factor(nets)
        out.append(
            {
                "month": month,
                "trades": len(mine),
                "traded_days": len(daily),
                "net": round(sum(nets), 2),
                "win_rate": pct(sum(1 for v in nets if v > 0), len(nets)),
                "profit_factor": None if pf is None else round(pf, 2),
                "profitable_days_pct": pct(sum(1 for v in daily.values() if v > 0), len(daily)),
            }
        )
    return out


# ---------------------------------------------------------------- entry and exit quality


def bucket_stats(rows: list[dict], key) -> list[dict]:
    """Per-bucket trades, win rate, net P&L, net profit factor, net expectancy and mean net R.
    `key(row)` returns the bucket label or None to leave the row out. Output order: label order."""
    groups: dict = {}
    for r in rows:
        label = key(r)
        if label is not None:
            groups.setdefault(label, []).append(r)
    out = []
    for label in sorted(groups, key=lambda x: (isinstance(x, str), x)):
        mine = groups[label]
        nets = [float(r["net"]) for r in mine]
        rs = [float(r["net_r"]) for r in mine if r.get("net_r") is not None]
        pf = profit_factor(nets)
        out.append(
            {
                "label": label,
                "trades": len(mine),
                "win_rate": pct(sum(1 for v in nets if v > 0), len(nets)),
                "net": round(sum(nets), 2),
                "profit_factor": None if pf is None else round(pf, 2),
                "expectancy": round(sum(nets) / len(nets), 2),
                "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
                "r_trades": len(rs),
            }
        )
    return out


def _entry_hhmm(row: dict) -> str | None:
    value = str(row.get("opened_at") or "")
    return value[11:16] if len(value) >= 16 else None


def entry_quality(rows: list[dict]) -> dict:
    """Entry-time breakdowns on net results. Trades without a reliable entry time (a
    data_flag, or no opened_at) are left out and counted in `excluded`."""
    usable = [r for r in rows if not r.get("data_flag") and _entry_hhmm(r)]
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    def weekday(r):
        try:
            return date.fromisoformat(r["trade_date"]).weekday()
        except ValueError:
            return None

    out = {
        "by_hour": bucket_stats(usable, lambda r: int(_entry_hhmm(r)[:2])),
        "by_weekday": bucket_stats(rows, weekday),
        "by_direction": bucket_stats(rows, lambda r: r.get("direction")),
        "by_session_part": bucket_stats(
            usable,
            lambda r: "First hour" if _entry_hhmm(r) < FIRST_HOUR_END else "Later in the session",
        ),
        "entry_excluded": len(rows) - len(usable),
        "first_hour_ends": FIRST_HOUR_END,
    }
    for row in out["by_weekday"]:
        row["label"] = weekdays[row["label"]]
    return out


# ---------------------------------------------------------------- excursions (MAE / MFE)


def _quartiles(values: list[float]) -> list[float] | None:
    if not values:
        return None
    return [round(float(v), 3) for v in np.percentile(values, [25, 50, 75])]


def _excursion_block(rows: list[dict]) -> dict:
    """Excursion figures for rows that carry rupee mfe, mae, risk_amount and realized_pnl."""
    r_rows = [r for r in rows if (r.get("risk_amount") or 0) > 0]
    mfe_r = [float(r["mfe"]) / float(r["risk_amount"]) for r in r_rows]
    mae_r = [float(r["mae"]) / float(r["risk_amount"]) for r in r_rows]
    winners = [r for r in rows if float(r["realized_pnl"]) > 0 and float(r["mfe"]) > 0]
    paid = sum(float(r["realized_pnl"]) for r in winners)
    offered = sum(float(r["mfe"]) for r in winners)
    big = [r for r in r_rows if float(r["mfe"]) / float(r["risk_amount"]) >= MFE_MEANINGFUL_R]
    gave_back = [r for r in big if float(r["net"]) < 0]

    def avg(vals):
        return round(sum(vals) / len(vals), 3) if vals else None

    win_mae = [
        float(r["mae"]) / float(r["risk_amount"]) for r in r_rows if float(r["realized_pnl"]) > 0
    ]
    lose_mae = [
        float(r["mae"]) / float(r["risk_amount"]) for r in r_rows if float(r["realized_pnl"]) < 0
    ]
    return {
        "trades": len(rows),
        "r_trades": len(r_rows),
        "avg_mfe_r": avg(mfe_r),
        "avg_mae_r": avg(mae_r),
        "mfe_r_quartiles": _quartiles(mfe_r),
        "mfe_capture_pct": pct(paid, offered),
        "winners_avg_mae_r": avg(win_mae),
        "losers_avg_mae_r": avg(lose_mae),
        "mae_reached_stop_pct": pct(sum(1 for v in mae_r if v >= 1.0), len(mae_r)),
        "reached_meaningful_mfe": len(big),
        "gave_back_pct": pct(len(gave_back), len(big)),
    }


def excursion_stats(rows: list[dict]) -> dict:
    """Excursion quality on rows with rupee `mfe`/`mae` (both None when unknown).

    MFE and MAE are upper bounds from one-minute bars: full-bar highs and lows, not
    executable prices at the traded quantity. Rows with a data_flag are left out and
    counted. R figures need the signal stop (`risk_amount`).

    mfe_capture_pct = realized profit of winners / their MFE (how much of the offered
    favourable move was kept). gave_back_pct = among trades whose MFE reached
    MFE_MEANINGFUL_R, the share that closed at a net loss. mae_reached_stop_pct = share
    whose adverse excursion reached the initial stop distance (1R).
    """
    covered = [r for r in rows if r.get("mfe") is not None and r.get("mae") is not None]
    usable = [r for r in covered if not r.get("data_flag")]
    out = {
        "total": len(rows),
        "covered": len(covered),
        "excluded_flagged": len(covered) - len(usable),
        "meaningful_mfe_r": MFE_MEANINGFUL_R,
        "basis": "upper bound from one-minute bars; not executable at the traded quantity",
        **_excursion_block(usable),
        "by_strategy": {},
    }
    for name in sorted({r["strategy"] for r in usable}):
        out["by_strategy"][name] = _excursion_block([r for r in usable if r["strategy"] == name])
    return out


# ---------------------------------------------------------------- execution quality


def slippage_summary(rows: list[dict]) -> dict:
    """Entry slippage versus the alert price, adverse and favourable shown separately.

    `entry_slippage` is rupees, positive = worse than the alert. It is already inside the
    fills the P&L uses. Exit slippage and live fills are not measured here.
    """
    measured = [r for r in rows if r.get("entry_slippage") is not None]

    def block(items):
        adverse = sum(float(r["entry_slippage"]) for r in items if float(r["entry_slippage"]) > 0)
        favourable = sum(
            float(r["entry_slippage"]) for r in items if float(r["entry_slippage"]) < 0
        )
        value = sum(float(r["entry_price"]) * float(r["closed_quantity"]) for r in items)
        net = adverse + favourable
        return {
            "trades": len(items),
            "net": round(net, 2),
            "adverse_total": round(adverse, 2),
            "favourable_total": round(favourable, 2),
            "adverse_trades": sum(1 for r in items if float(r["entry_slippage"]) > 0),
            "favourable_trades": sum(1 for r in items if float(r["entry_slippage"]) < 0),
            "net_bps": round(10000.0 * net / value, 2) if value > 0 else None,
            "adverse_bps": round(10000.0 * adverse / value, 2) if value > 0 else None,
        }

    out = {
        "covered": len(measured),
        "total": len(rows),
        "basis": (
            "entry fill against the signal alert price; exit slippage and live fills are "
            "not measured, and charges are estimates, not broker-reported"
        ),
        **(block(measured) if measured else {}),
        "by_strategy": {},
        "by_symbol": [],
    }
    for name in sorted({r["strategy"] for r in measured}):
        out["by_strategy"][name] = block([r for r in measured if r["strategy"] == name])
    by_symbol = []
    for sym in sorted({r["symbol"] for r in measured}):
        b = block([r for r in measured if r["symbol"] == sym])
        by_symbol.append({"symbol": sym, **b})
    by_symbol.sort(key=lambda x: x["adverse_total"], reverse=True)
    out["by_symbol"] = by_symbol[:10]
    return out


# ---------------------------------------------------------------- comparison


def daily_correlation(series: dict[str, np.ndarray]) -> dict:
    """Pearson correlation of daily net P&L between strategies over the same sessions.
    An entry is None when a series is flat or fewer than MIN_CORRELATION_SESSIONS sessions."""
    names = sorted(series)
    matrix: list[list[float | None]] = []
    for a in names:
        row = []
        for b in names:
            x, y = np.asarray(series[a], float), np.asarray(series[b], float)
            if a == b:
                row.append(1.0)
            elif len(x) < MIN_CORRELATION_SESSIONS or _is_flat(x) or _is_flat(y):
                row.append(None)
            else:
                row.append(round(float(np.corrcoef(x, y)[0, 1]), 3))
        matrix.append(row)
    return {"strategies": names, "matrix": matrix, "sessions": len(next(iter(series.values()), []))}


def portfolio_contribution(series: dict[str, np.ndarray]) -> list[dict]:
    """Each strategy's net, share of total, and the portfolio's drawdown with and without it.
    A positive `drawdown_change` means the portfolio drew down LESS with the strategy."""
    names = sorted(series)
    total = sum(np.asarray(series[n], float) for n in names) if names else np.array([])
    full_dd = max_drawdown_value(total) if names else 0.0
    out = []
    for name in names:
        rest = [np.asarray(series[n], float) for n in names if n != name]
        without = sum(rest) if rest else np.zeros(len(total))
        dd_without = max_drawdown_value(without)
        net = float(np.sum(series[name]))
        out.append(
            {
                "strategy": name,
                "net": round(net, 2),
                "share_of_net_pct": pct(net, float(total.sum())),
                "portfolio_dd_with": round(full_dd, 2),
                "portfolio_dd_without": round(dd_without, 2),
                "drawdown_change": round(full_dd - dd_without, 2),
            }
        )
    return out


# ---------------------------------------------------------------- grading

# Lower edge of each band, in order poor | acceptable | good | excellent. Context-free
# screening bands, not guarantees. Change them here and nowhere else.
BENCHMARK_BANDS = {
    "net_profit_factor": {"edges": (1.0, 1.3, 2.0), "higher_is_better": True},
    "win_rate_pct": {"edges": (40.0, 50.0, 60.0), "higher_is_better": True},
    "expectancy_r": {"edges": (0.0, 0.10, 0.30), "higher_is_better": True},
    "sharpe": {"edges": (0.5, 1.0, 2.0), "higher_is_better": True},
    "sortino": {"edges": (1.0, 1.5, 3.0), "higher_is_better": True},
    "recovery_factor": {"edges": (1.0, 2.0, 3.0), "higher_is_better": True},
    "cost_pct_of_gross": {"edges": (10.0, 20.0, 30.0), "higher_is_better": False},
}
GRADES = ("poor", "acceptable", "good", "excellent")
METRIC_LABELS = {
    "net_profit_factor": "net profit factor",
    "win_rate_pct": "win rate",
    "expectancy_r": "expectancy in R",
    "sharpe": "Sharpe",
    "sortino": "Sortino",
    "recovery_factor": "recovery factor",
    "cost_pct_of_gross": "charges as a share of gross profit",
}
# Metrics that may set the overall grade. Win rate never does: a high win rate alone
# says nothing about whether the average trade makes money.
OVERALL_METRICS = (
    "net_profit_factor",
    "expectancy_r",
    "cost_pct_of_gross",
    "recovery_factor",
    "sharpe",
    "sortino",
)
# Risk metrics need a long enough daily history; return metrics need enough trades.
RISK_METRICS = ("recovery_factor", "sharpe", "sortino")


def grade_value(metric: str, value: float | None) -> str | None:
    """Band for a value: poor below the first edge, acceptable from it, good from the second,
    excellent above the third (for cost, mirrored: lower is better). None for no value."""
    if value is None or not math.isfinite(value):
        return None
    band = BENCHMARK_BANDS[metric]
    e0, e1, e2 = band["edges"]
    if band["higher_is_better"]:
        idx = 0 + (value >= e0) + (value >= e1) + (value > e2)
    else:
        idx = 3 if value < e0 else 2 if value < e1 else 1 if value <= e2 else 0
    return GRADES[idx]


def grade_report(values: dict[str, float | None], trades: int, sessions: int) -> dict:
    """Grades each metric when its sample allows, and an overall grade only when the
    profitability and the risk metrics are all gradable.

    Overall is the WEAKEST graded metric among OVERALL_METRICS, so a few excellent numbers
    cannot lift it. Return metrics need MIN_TRADES_FOR_GRADE trades; risk metrics need
    MIN_SESSIONS sessions. A metric without enough data is reported with the reason and
    does not count towards the overall grade, but the overall grade is withheld unless
    the core set (profit factor, expectancy in R, cost share, recovery factor) is graded.
    Without the risk metrics, `preliminary` is the weakest of the three return metrics
    (30 trades and 10 sessions) and `assessment` is "preliminary", never "validated".
    """
    metrics: dict[str, dict] = {}
    for name in BENCHMARK_BANDS:
        value = values.get(name)
        reason = None
        if value is None:
            reason = "not available"
        elif name in RISK_METRICS and sessions < MIN_SESSIONS:
            reason = f"needs {MIN_SESSIONS} trading sessions, has {sessions}"
        elif name not in RISK_METRICS and trades < MIN_TRADES_FOR_GRADE:
            reason = f"needs {MIN_TRADES_FOR_GRADE} trades, has {trades}"
        elif name not in RISK_METRICS and sessions < MIN_SESSIONS_FOR_RETURN_GRADE:
            reason = f"needs {MIN_SESSIONS_FOR_RETURN_GRADE} trading sessions, has {sessions}"
        metrics[name] = {
            "metric_value": value,
            "grade": None if reason else grade_value(name, value),
            "reason": reason,
        }
    core = ("net_profit_factor", "expectancy_r", "cost_pct_of_gross", "recovery_factor")
    missing = [n for n in core if metrics[n]["grade"] is None]
    graded = [metrics[n]["grade"] for n in OVERALL_METRICS if metrics[n]["grade"] is not None]
    overall = None if missing else GRADES[min(GRADES.index(g) for g in graded)]
    # Preliminary: return metrics only, when the risk metrics cannot yet be graded. It says
    # how the trades look so far; it is not evidence of a proven edge.
    return_core = ("net_profit_factor", "expectancy_r", "cost_pct_of_gross")
    preliminary = None
    if overall is None and all(metrics[n]["grade"] is not None for n in return_core):
        preliminary = GRADES[min(GRADES.index(metrics[n]["grade"]) for n in return_core)]
    return {
        "metrics": metrics,
        "overall": overall,
        "preliminary": preliminary,
        "assessment": "validated" if overall else "preliminary" if preliminary else "none",
        "overall_note": (
            None
            if overall
            else "Not graded: "
            + ", ".join(f"{METRIC_LABELS[n]} ({metrics[n]['reason']})" for n in missing)
            + "."
        ),
        "sample": {"trades": trades, "sessions": sessions},
    }


# ---------------------------------------------------------------- validation status

# Thresholds for the validation checks. The performance and sample gates reuse the
# constants above (MIN_TRADES_FOR_GRADE, MIN_SESSIONS_FOR_RETURN_GRADE, MIN_SESSIONS).
VALIDATION_MIN_NET_PF = 1.3  # net profit factor for the validated level (the "good" band)
VALIDATION_MAX_BEST_DAY_SHARE_PCT = 40.0
VALIDATION_COST_STRESS = 0.5  # estimated charges raised by half
VALIDATION_MIN_STRESSED_PF = 1.0
VALIDATION_MIN_SLIPPAGE_COVERAGE = 0.8

VALIDATION_LABELS = {
    "not_enough_data": "Not enough data yet",
    "not_encouraging": "Not encouraging so far",
    "preliminary": "Preliminary",
    "under_validation": "Under validation",
    "validated": "Validated",
}
_ENCOURAGING_IDS = ("net_pf_above_1", "net_expectancy_positive", "edge_not_negative")
_CHECK_RANK = {"fail": 0, "pending": 1, "not_assessed": 2, "pass": 3}


def stressed_profit_factor(rows: list[dict], extra_cost_share: float = VALIDATION_COST_STRESS):
    """Net profit factor if every trade's estimated charges were `extra_cost_share` higher.
    Rows carry `net` (after charges) and `cost`; an unknown cost counts as 0."""
    return profit_factor(
        float(r["net"]) - extra_cost_share * float(r.get("cost") or 0.0) for r in rows
    )


def slippage_coverage(rows: list[dict]) -> float | None:
    """Share of trades with a reliable entry time whose entry fill could be compared with the
    alert price. None when no trade has a reliable entry time."""
    reliable = [r for r in rows if not r.get("data_flag")]
    if not reliable:
        return None
    return sum(1 for r in reliable if r.get("entry_slippage") is not None) / len(reliable)


def _finite_or_none(value):
    return value if isinstance(value, int | float) and math.isfinite(value) else None


def _check(cid, dimension, label, status, detail, needed_for):
    return {
        "id": cid,
        "dimension": dimension,
        "label": label,
        "status": status,
        "detail": detail,
        "needed_for": needed_for,
    }


def validation_status(
    *,
    trades: int,
    sessions: int,
    net_pf: float | None,
    net_expectancy: float | None,
    edge_verdict: str | None,
    concentration_stats: dict | None,
    drawdown: dict | None,
    last_exp20: float | None,
    stressed_pf: float | None,
    slippage_coverage_share: float | None,
    mode: str | None,
) -> dict:
    """How far the observed results can be trusted: one status from many named checks.

    Status: `not_enough_data` (under 30 trades or 10 sessions, or a profit figure is
    unavailable), `not_encouraging` (enough data, but net profit factor is not above 1, net
    expectancy is not positive, or the edge check does not show a positive average),
    `preliminary` (encouraging and the edge check has not failed, but under 60 sessions or the
    edge check cannot judge yet),
    `under_validation` (60 sessions and a verdict of at least positive-but-inconclusive, with
    other checks failed, pending or not assessed) and `validated` (EVERY check passes).

    Four dimensions: performance, statistical, robustness, execution. Each check is `pass`,
    `fail`, `pending` (more data would settle it) or `not_assessed` (the evidence is
    unavailable). Missing evidence never passes. A failed check blocks `validated` but never
    lowers a status or hides the result. Execution cannot be assessed for Sandbox fills.
    Detail text carries percentages and counts only, so rupee scaling never makes it stale.
    """
    # a non-finite number is unavailable evidence, never a pass or a failure
    net_pf, net_expectancy, last_exp20, stressed_pf, slippage_coverage_share = (
        _finite_or_none(v)
        for v in (net_pf, net_expectancy, last_exp20, stressed_pf, slippage_coverage_share)
    )
    conc = concentration_stats or {}
    dd = drawdown or {}
    enough = trades >= MIN_TRADES_FOR_GRADE and sessions >= MIN_SESSIONS_FOR_RETURN_GRADE
    waiting = "Not assessed until there are enough trades and sessions."

    def settled(cid, dim, label, status, detail, needed):
        # below the sample minimum nothing else is judged: it would be noise
        if not enough:
            return _check(cid, dim, label, "pending", waiting, needed)
        return _check(cid, dim, label, status, detail, needed)

    # every trade made the same positive result: the edge check returns inconclusive on it
    flat_positive = (
        edge_verdict == VERDICT_INCONCLUSIVE and net_expectancy is not None and net_expectancy > 0
    )
    checks = [
        _check(
            "sample",
            "performance",
            "Enough trades and sessions",
            "pass" if enough else "pending",
            f"Needs {MIN_TRADES_FOR_GRADE} trades over {MIN_SESSIONS_FOR_RETURN_GRADE} trading "
            f"sessions; this selection has {trades} trades over {sessions} sessions.",
            "preliminary",
        ),
        settled(
            "net_pf_above_1",
            "performance",
            "Net profit factor above 1.0",
            "not_assessed" if net_pf is None else "pass" if net_pf > 1.0 else "fail",
            "Net profit factor is not available (no losing trade or no data)."
            if net_pf is None
            else f"Net profit factor after charges is {net_pf:.3f}.",
            "preliminary",
        ),
        settled(
            "net_expectancy_positive",
            "performance",
            "Positive net result per trade",
            "not_assessed" if net_expectancy is None else "pass" if net_expectancy > 0 else "fail",
            "The average result per trade after charges is not available."
            if net_expectancy is None
            else "The average result per trade after charges is "
            + ("positive." if net_expectancy > 0 else "not positive."),
            "preliminary",
        ),
        settled(
            "net_pf_validated",
            "performance",
            f"Net profit factor above {VALIDATION_MIN_NET_PF}",
            "not_assessed"
            if net_pf is None
            else "pass"
            if net_pf > VALIDATION_MIN_NET_PF
            else "fail",
            "Net profit factor is not available."
            if net_pf is None
            else f"Net profit factor is {net_pf:.4f}; the validated level needs more than "
            f"{VALIDATION_MIN_NET_PF}.",
            "validated",
        ),
    ]
    verdict = edge_verdict
    if verdict is None:
        nn_status, nn_detail = "not_assessed", "The edge check was not run."
        sup_status, sup_detail = nn_status, nn_detail
    elif verdict == VERDICT_SUPPORTED:
        nn_status, nn_detail = "pass", "The edge check says the average is positive."
        sup_status, sup_detail = "pass", "The edge check supports a positive average."
    elif verdict == VERDICT_POSITIVE_INCONCLUSIVE:
        nn_status, nn_detail = "pass", "The edge check says the average is positive."
        sup_status = "pending"
        sup_detail = "Positive so far, but the evidence is not strong enough to rule out luck."
    elif verdict == VERDICT_INSUFFICIENT:
        nn_status = sup_status = "pending"
        nn_detail = sup_detail = "The edge check has too few trading days to say."
    elif flat_positive:
        # still not encouraging (the check cannot show a positive average), but say why
        nn_status = sup_status = "fail"
        nn_detail = sup_detail = (
            "Every trade made the same positive result, so the edge check has no spread to "
            "test and cannot show that the average is real."
        )
    elif verdict == VERDICT_NEGATIVE:
        nn_status = sup_status = "fail"
        nn_detail = sup_detail = "The edge check says the average is negative."
    else:  # inconclusive on an average that is not positive
        nn_status = sup_status = "fail"
        nn_detail = sup_detail = (
            "The average is not positive, and the evidence does not settle whether it is "
            "negative or just noise."
        )
    checks.append(
        settled(
            "edge_not_negative",
            "statistical",
            "Edge at least positive but inconclusive",
            nn_status,
            nn_detail,
            "under_validation",
        )
    )
    checks.append(
        settled(
            "edge_supported",
            "statistical",
            "Edge statistically supported",
            sup_status,
            sup_detail,
            "validated",
        )
    )
    checks.append(
        _check(
            "history",
            "robustness",
            f"{MIN_SESSIONS} trading sessions of history",
            "pass" if sessions >= MIN_SESSIONS else "pending",
            f"{sessions} of {MIN_SESSIONS} trading sessions.",
            "under_validation",
        )
    )
    top1 = conc.get("top1_days_share_pct")
    if not conc:
        top1_status, top1_detail = "not_assessed", "Day-by-day results are not available."
    elif top1 is None:
        top1_status, top1_detail = "fail", "There is no net profit to spread across days."
    else:
        top1_status = "pass" if top1 < VALIDATION_MAX_BEST_DAY_SHARE_PCT else "fail"
        top1_detail = (
            f"The best day is {top1:.1f}% of net profit; the limit is "
            f"{VALIDATION_MAX_BEST_DAY_SHARE_PCT:.0f}%."
        )
    checks.append(
        settled(
            "best_day_share",
            "robustness",
            "No single day carries the profit",
            top1_status,
            top1_detail,
            "validated",
        )
    )
    for cid, key, label, none_status, none_detail in (
        (
            "positive_ex_best_day",
            "net_ex_best_day",
            "Still profitable without the best day",
            "not_assessed",
            "Day-by-day results are not available.",
        ),
        (
            "positive_ex_best_five",
            "net_ex_best_five_days",
            "Still profitable without the best five days",
            "pending",
            "Needs more than five traded days.",
        ),
    ):
        val = conc.get(key)
        none_s = none_status if conc else "not_assessed"
        checks.append(
            settled(
                cid,
                "robustness",
                label,
                none_s if val is None else "pass" if val > 0 else "fail",
                none_detail
                if val is None
                else "Profit stays positive without those days."
                if val > 0
                else "Profit is not positive without those days.",
                "validated",
            )
        )
    checks.append(
        settled(
            "recent_expectancy",
            "robustness",
            "Last 20 trades still positive",
            "pending" if last_exp20 is None else "pass" if last_exp20 > 0 else "fail",
            "Needs 20 trades."
            if last_exp20 is None
            else "The average of the last 20 trades is "
            + ("positive." if last_exp20 > 0 else "not positive."),
            "validated",
        )
    )
    if "max_drawdown" not in dd:
        rec_status, rec_detail = "not_assessed", "Drawdown is not available."
    elif dd["max_drawdown"] == 0:
        rec_status, rec_detail = "pass", "There has been no drawdown."
    elif dd.get("drawdown_recovered"):
        rec_status, rec_detail = "pass", "The largest drawdown has been recovered."
    else:
        rec_status, rec_detail = "fail", "The largest drawdown has not been recovered yet."
    checks.append(
        settled(
            "drawdown_recovered",
            "robustness",
            "Largest drawdown recovered",
            rec_status,
            rec_detail,
            "validated",
        )
    )
    is_live = mode == "live"
    checks.append(
        _check(
            "live_fills",
            "execution",
            "Evidence from live fills",
            "pass" if is_live else "not_assessed",
            "Results come from live trading."
            if is_live
            else "Sandbox fills are simulated, so they do not establish how live orders would "
            "execute."
            if mode
            else "The trading mode is not known.",
            "validated",
        )
    )
    checks.append(
        settled(
            "cost_stress",
            "execution",
            "Still profitable if charges were 50% higher",
            "not_assessed"
            if stressed_pf is None
            else "pass"
            if stressed_pf > VALIDATION_MIN_STRESSED_PF
            else "fail",
            "The stressed profit factor is not available."
            if stressed_pf is None
            else f"Net profit factor with charges 50% higher is {stressed_pf:.3f}.",
            "validated",
        )
    )
    cov = slippage_coverage_share
    checks.append(
        settled(
            "slippage_coverage",
            "execution",
            "Entry slippage measured on most trades",
            "not_assessed"
            if cov is None
            else "pass"
            if cov >= VALIDATION_MIN_SLIPPAGE_COVERAGE
            else "fail",
            "No trade has a reliable entry time, so slippage cannot be measured."
            if cov is None
            else f"Entry slippage is measured on {cov * 100:.1f}% of trades with a reliable entry "
            f"time; at least {VALIDATION_MIN_SLIPPAGE_COVERAGE * 100:.0f}% is needed.",
            "validated",
        )
    )

    by_id = {c["id"]: c["status"] for c in checks}
    sample_ok = by_id["sample"] == "pass"
    prelim_ok = sample_ok and all(
        by_id[i] == "pass" for i in ("net_pf_above_1", "net_expectancy_positive")
    )
    uv_ok = prelim_ok and by_id["history"] == "pass" and by_id["edge_not_negative"] == "pass"
    all_ok = all(c["status"] == "pass" for c in checks)
    perf_ids = ("net_pf_above_1", "net_expectancy_positive")
    edge_failed = by_id["edge_not_negative"] == "fail"
    if not sample_ok:
        status = "not_enough_data"
    elif edge_failed or any(by_id[i] == "fail" for i in perf_ids):
        # Profit figures that are not positive, or an edge check that does not show a positive
        # average (negative, or inconclusive on a non-positive average): not encouraging, at
        # any history length. An edge check that cannot judge yet is pending, not failed.
        status = "not_encouraging"
    elif not prelim_ok:
        status = "not_enough_data"  # a performance figure is unavailable: not judged, not passed
    elif all_ok:
        status = "validated"
    elif uv_ok:
        status = "under_validation"
    else:
        status = "preliminary"
    next_level = {
        "not_enough_data": "preliminary",
        "not_encouraging": "preliminary",
        "preliminary": "under_validation",
        "under_validation": "validated",
        "validated": None,
    }[status]
    blocking = [c for c in checks if c["needed_for"] == next_level and c["status"] != "pass"]
    if status == "not_enough_data" and not sample_ok:
        blocking = [c for c in blocking if c["id"] == "sample"]  # the rest waits on the sample
    if status == "not_encouraging":
        blocking = [c for c in checks if c["status"] == "fail" and c["id"] in _ENCOURAGING_IDS]
    dimensions = {}
    for dim in ("performance", "statistical", "robustness", "execution"):
        mine = [c["status"] for c in checks if c["dimension"] == dim]
        dimensions[dim] = min(mine, key=lambda s: _CHECK_RANK[s])
    failed = [c for c in checks if c["status"] == "fail"]
    waiting_on = [c for c in checks if c["status"] in ("pending", "not_assessed")]
    summaries = {
        "not_enough_data": (
            f"Too early to judge. At least {MIN_TRADES_FOR_GRADE} trades over "
            f"{MIN_SESSIONS_FOR_RETURN_GRADE} trading sessions are needed; there are {trades} "
            f"trades over {sessions} sessions."
            if not sample_ok
            else "The profit figures for this selection are not available, so it cannot be "
            "assessed yet."
        ),
        "not_encouraging": (
            "There is enough data to look at, but the results are not encouraging so far: "
            + (
                "the edge check says the average result per trade is negative."
                if verdict == VERDICT_NEGATIVE
                else "the edge check does not show a positive average result per trade."
                if edge_failed
                else "net profit factor and the average result per trade need to be above "
                "their break-even levels."
            )
        ),
        "preliminary": "Early results are encouraging, but the history is not long enough, or "
        "the edge check cannot judge yet, for a deeper review.",
        "under_validation": "The average is positive and the history is long enough for a "
        f"deeper review. {len(failed)} check(s) failed and {len(waiting_on)} are pending or "
        "not assessed.",
        "validated": "Every performance, statistical, robustness and execution check passes. "
        "This describes the evidence so far and is not a guarantee of future results.",
    }
    return {
        "status": status,
        "label": VALIDATION_LABELS[status],
        "summary": summaries[status],
        "next_level": next_level,
        "next_level_label": VALIDATION_LABELS.get(next_level) if next_level else None,
        "dimensions": dimensions,
        "checks": checks,
        "failed": [c["id"] for c in failed],
        "waiting": [c["id"] for c in waiting_on],
        "blocking": [c["id"] for c in blocking],
        "passed_count": sum(1 for c in checks if c["status"] == "pass"),
        "total_count": len(checks),
        "mode": mode,
    }
