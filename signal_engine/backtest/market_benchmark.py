"""Market benchmark for a recorded intraday run: read-only, report only.

Design (proposed 2026-10-09, for review before any recording integration):

  QUESTION.    Does the strategy earn more than a passive position in the same market over the
               same dates, times and universe, once both pay the same costs? A raw difference in
               returns is not alpha, so the report also separates the market's own move.
  SOURCE.      A run's `evidence/trades.parquet` and the published 5-minute bars of the dataset
               version the run recorded. Nothing is simulated and the run record is never written.
  MARKET.      Per 5-minute bar, the equal-weight mean of (Close / Open - 1) over every symbol of
               the loaded universe that has a bar at that time, if at least MIN_SYMBOLS do.
               The universe is the run's own eligible set, which is today's F&O list applied to
               the past (disclosed survivorship, shared by strategy and benchmark).
  WINDOW.      For each trade, the bars from its entry bar to its exit bar, both inclusive. The
               fill is the open of the entry bar, so the benchmark is bought at the same open.
               The exit bar is counted whole; the sensitivity `exit_bar_excluded` drops it. The true exit lies inside that bar, so the two readings bracket the answer; both are computed on exactly the same trades. A
               trade whose window has a missing or thin bar is left out and counted.
  BENCHMARKS.  market_long       the market over the trade's window, held long.
               direction_matched the same window, in the trade's own direction (long when the
                                 trade is a buy, short when a sell): the market exposure the
                                 trade carried, with no stock selection.
  COSTS.       Both benchmarks pay the strategy's own recorded round-trip cost on every trade and
               use the same notional per trade, so cost and exposure are equal by construction and
               cancel in the relative figure. A real index instrument would cost less.
  STATISTICS.  Every mean is trade-weighted. Its standard error is clustered by IST session (CR0:
               the square root of the sum over days of the squared within-day deviation sums,
               divided by the trade count), so the mean, interval and t-statistic agree.
  RELATIVE.    relative = trade gross bps - direction-matched gross bps, per trade. A regression of
               trade gross bps on direction-matched bps gives alpha (intercept) and beta (slope).
               Trades on one day share the same market move, hence the clustering. The intercept is a sample
               description, not proof of skill: it carries no adjustment for risk, sector or style.
  GATING.      None. Report only; no trial is created.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.cost_contribution import seal
from signal_engine.timeutils import IST

SCHEMA = "market_benchmark/1"
BAR_MINUTES = 5
MIN_SYMBOLS = 30
MIN_GROUP = 30
BATCH = 25
REQUIRED = ("symbol", "side", "entry_time", "exit_time", "gross_bps", "net_bps", "cost_bps")


class BenchmarkError(ValueError):
    """The inputs cannot support the benchmark."""


def _num(x: Any) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def market_bar_returns(
    bars: Mapping[str, pd.DataFrame], min_symbols: int = MIN_SYMBOLS
) -> pd.DataFrame:
    """Equal-weight mean bar return per timestamp (UTC ns index) and the symbols behind it."""
    total = pd.Series(dtype=float)
    count = pd.Series(dtype=float)
    symbols = sorted(bars)
    for i in range(0, len(symbols), BATCH):
        parts = []
        for sym in symbols[i : i + BATCH]:
            df = bars[sym]
            idx = pd.DatetimeIndex(df.index)
            idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
            o, c = df["Open"].to_numpy(float), df["Close"].to_numpy(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                r = np.where(o > 0, c / o - 1.0, np.nan)
            s = pd.Series(r, index=idx.astype("int64").to_numpy())
            parts.append(s[np.isfinite(s)])
        if not parts:
            continue
        g = pd.concat(parts).groupby(level=0)
        total = total.add(g.sum(), fill_value=0.0)
        count = count.add(g.count().astype(float), fill_value=0.0)
    out = pd.DataFrame({"n": count, "ret": total / count}).sort_index()
    out.loc[out["n"] < min_symbols, "ret"] = np.nan
    return out


def interval_returns(
    trades: pd.DataFrame, market: pd.DataFrame, include_exit_bar: bool = True
) -> pd.DataFrame:
    """Market return over each trade's bars. Columns: market_ret (fraction) and status."""
    ts = market.index.to_numpy("int64")
    r = market["ret"].to_numpy(float)
    valid = np.isfinite(r)
    log = np.where(valid, np.log1p(np.where(valid, r, 0.0)), 0.0)
    cs = np.concatenate([[0.0], np.cumsum(log)])
    bad = np.concatenate([[0], np.cumsum(~valid)])
    en = pd.to_datetime(trades["entry_time"], utc=True).astype("int64").to_numpy()
    ex = pd.to_datetime(trades["exit_time"], utc=True).astype("int64").to_numpy()
    step = BAR_MINUTES * 60 * 10**9
    n = len(trades)
    ret = np.full(n, np.nan)
    status = np.full(n, "ok", dtype=object)
    for i in range(n):
        a = int(np.searchsorted(ts, en[i]))
        b = int(np.searchsorted(ts, ex[i]))
        if a >= len(ts) or ts[a] != en[i] or b >= len(ts) or ts[b] != ex[i] or b < a:
            status[i] = "bar_missing"
            continue
        if b - a + 1 != (ex[i] - en[i]) // step + 1:
            status[i] = "path_gap"
            continue
        last = b if include_exit_bar else b - 1
        if last < a:
            ret[i] = 0.0
            continue
        if bad[last + 1] - bad[a] > 0:
            status[i] = "thin_market_bar"
            continue
        ret[i] = np.expm1(cs[last + 1] - cs[a])
    return pd.DataFrame({"market_ret": ret, "status": status}, index=trades.index)


def _clustered_mean(x: np.ndarray, day: np.ndarray) -> dict[str, Any]:
    """Trade-weighted mean of x with a day-clustered (CR0) standard error."""
    n = len(x)
    mean = float(np.mean(x))
    codes = pd.factorize(day)[0]
    k = int(codes.max()) + 1 if n else 0
    out: dict[str, Any] = {"mean": _num(mean), "days": k, "t": None, "ci95": None}
    if k < 3:
        return out
    per_day = np.bincount(codes, weights=x - mean, minlength=k)
    se = float(np.sqrt((per_day**2).sum()) / n)
    if se == 0:
        return out
    out.update(se=_num(se), t=_num(mean / se), ci95=[_num(mean - 1.96 * se), _num(mean + 1.96 * se)])
    return out


def _cluster_ols(y: np.ndarray, x: np.ndarray, day: np.ndarray) -> dict[str, Any]:
    """y = a + b x with day-clustered (CR0) standard errors."""
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    xtx = X.T @ X
    if n < 10 or np.linalg.cond(xtx) > 1e12:
        return {"alpha_bps": None, "beta": None, "note": "regression not estimable"}
    inv = np.linalg.inv(xtx)
    coef = inv @ (X.T @ y)
    u = y - X @ coef
    codes = pd.factorize(day)[0]
    k = int(codes.max()) + 1
    score = np.zeros((k, 2))
    np.add.at(score, codes, X * u[:, None])
    cov = inv @ (score.T @ score) @ inv
    se = np.sqrt(np.diag(cov))
    return {
        "alpha_bps": _num(coef[0]),
        "alpha_se": _num(se[0]),
        "alpha_t": _num(coef[0] / se[0]) if se[0] > 0 else None,
        "beta": _num(coef[1]),
        "beta_se": _num(se[1]),
        "days": k,
        "note": "intercept describes this sample; no risk, sector or style adjustment",
    }


def _group_table(
    keys: np.ndarray, net: np.ndarray, dm_net: np.ndarray, rel: np.ndarray, day: np.ndarray
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for key in sorted(set(keys)):
        sel = keys == key
        if sel.sum() < MIN_GROUP:
            groups[str(key)] = {"trades": int(sel.sum()), "withheld": f"fewer than {MIN_GROUP} trades"}
            continue
        groups[str(key)] = {
            "trades": int(sel.sum()),
            "strategy_net_bps": _num(net[sel].mean()),
            "direction_matched_net_bps": _num(dm_net[sel].mean()),
            "relative_gross_bps": _clustered_mean(rel[sel], day[sel]),
        }
    return groups


def usable(interval: pd.DataFrame) -> np.ndarray:
    """Trades whose market window is complete."""
    return ((interval["status"] == "ok") & interval["market_ret"].notna()).to_numpy()


METRICS = {
    "unit": "basis points of the trade notional, per trade, trade-weighted mean",
    "t": "mean divided by its day-clustered standard error (a unitless statistic, not bps)",
    "se": "bps, day-clustered (CR0)",
    "ci95": "bps, mean +/- 1.96 se",
    "gross": "price move of the trade before cost, bps of entry price",
    "net": "gross minus the trade's recorded round-trip cost (cost_bps)",
    "market": "equal-weight market return over the trade's bars, bps",
    "direction_matched": "direction (+1 buy, -1 sell) times market; net also pays the same cost_bps",
    "relative_gross_bps": "strategy gross minus direction-matched gross; equals strategy net minus direction-matched net",
}


def _reconcile(trades: pd.DataFrame, ok: np.ndarray) -> dict[str, Any]:
    """Means over all recorded trades, the compared sample and the excluded remainder."""
    def block(t: pd.DataFrame) -> dict[str, Any]:
        return {
            "trades": int(len(t)),
            "mean_gross_bps": _num(t["gross_bps"].mean()) if len(t) else None,
            "mean_cost_bps": _num(t["cost_bps"].mean()) if len(t) else None,
            "mean_net_bps": _num(t["net_bps"].mean()) if len(t) else None,
        }

    err = (trades["net_bps"] - (trades["gross_bps"] - trades["cost_bps"])).abs().max()
    return {
        "all_recorded_trades": block(trades),
        "compared_sample": block(trades[ok]),
        "excluded_sample": block(trades[~ok]),
        "net_equals_gross_minus_cost_max_abs_error_bps": _num(err),
    }


def compare(
    trades: pd.DataFrame, interval: pd.DataFrame, sample: np.ndarray | None = None
) -> dict[str, Any]:
    """Absolute and benchmark-relative results over the trades with a defined market window.

    `sample` further restricts the trades (a boolean mask over `trades`), so two readings of the
    market window can be compared on exactly the same trades."""
    missing = [c for c in REQUIRED if c not in trades.columns]
    if missing:
        raise BenchmarkError(f"trade list lacks columns: {', '.join(missing)}")
    if trades.empty:
        raise BenchmarkError("trade list is empty")
    ok = usable(interval) if sample is None else usable(interval) & sample
    t = trades[ok]
    out: dict[str, Any] = {
        "trades_total": int(len(trades)),
        "trades_compared": int(len(t)),
        "status_counts": {str(k): int(v) for k, v in interval["status"].value_counts().items()},
        "metrics": METRICS,
        "reconciliation": _reconcile(trades, ok),
    }
    if t.empty:
        return out
    m = interval["market_ret"].to_numpy(float)[ok] * 1e4
    d = np.where(t["side"].to_numpy() == "BUY", 1.0, -1.0)
    entry = pd.to_datetime(t["entry_time"], utc=True).dt.tz_convert(IST)
    day = entry.dt.date.astype(str).to_numpy()
    gross, net, cost = (t[c].to_numpy(float) for c in ("gross_bps", "net_bps", "cost_bps"))
    dm = d * m
    series = {
        "strategy_gross_bps": gross,
        "strategy_net_bps": net,
        "market_long_gross_bps": m,
        "market_long_net_bps": m - cost,
        "direction_matched_gross_bps": dm,
        "direction_matched_net_bps": dm - cost,
        "relative_gross_bps": gross - dm,
        "strategy_net_minus_market_long_net_bps": net - (m - cost),
    }
    out["costs"] = "both benchmarks pay the strategy's own recorded round-trip cost per trade"
    out["mean_direction"] = _num(d.mean())
    out["share_long"] = _num((d > 0).mean())
    out["results"] = {k: _clustered_mean(v, day) for k, v in series.items()}
    out["share_trades_beating_direction_matched"] = _num((gross - dm > 0).mean())
    out["regression_gross_on_direction_matched"] = _cluster_ols(gross, dm, day)
    rel = gross - dm
    out["by_side"] = _group_table(t["side"].to_numpy(), net, dm - cost, rel, day)
    out["by_year"] = _group_table(entry.dt.year.to_numpy(), net, dm - cost, rel, day)
    return out


def build_evidence(
    trades: pd.DataFrame, bars: Mapping[str, pd.DataFrame], source: dict[str, Any]
) -> dict[str, Any]:
    market = market_bar_returns(bars)
    incl = interval_returns(trades, market, include_exit_bar=True)
    excl = interval_returns(trades, market, include_exit_bar=False)
    common = usable(incl) & usable(excl)  # both readings on exactly the same trades
    main_cmp = compare(trades, incl, common)
    sens = compare(trades, excl, common)
    body = {
        "schema": SCHEMA,
        "report_only": True,
        "diagnostic_not_trials": True,
        "source": {
            **source,
            "market_bars": int(len(market)),
            "market_symbols_max": int(market["n"].max()),
            "min_symbols_per_bar": MIN_SYMBOLS,
            "universe": f"{len(bars)} symbols loaded for the run (current F&O list, survivorship disclosed)",
        },
        "limitations": [
            "survivorship: the universe is today's F&O list applied to the past, for the strategy and the benchmark alike",
            "the benchmark pays the strategy's own recorded cost; a real index instrument would cost less",
            "alpha and beta are sample descriptions with no risk, sector or style adjustment",
            "an equal-weight universe average is not an investable index",
        ],
        "comparison": main_cmp,
        "exit_bar_excluded": {
            "same_trades_as_comparison": True,
            **{
                k: sens.get(k)
                for k in ("trades_compared", "results", "regression_gross_on_direction_matched")
            },
        },
    }
    return {**body, "content_sha256": seal(body)}


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    problems = []
    if evidence.get("schema") != SCHEMA:
        problems.append(f"schema is {evidence.get('schema')!r}, expected {SCHEMA!r}")
    body = {k: v for k, v in evidence.items() if k != "content_sha256"}
    if evidence.get("content_sha256") != seal(body):
        problems.append("content_sha256 does not match the payload")
    if evidence.get("report_only") is not True or evidence.get("diagnostic_not_trials") is not True:
        problems.append("report_only and diagnostic_not_trials must both be true")
    return problems


def evidence_for_run(run_dir: str | Path, bars: Mapping[str, pd.DataFrame]) -> dict[str, Any]:
    run = Path(run_dir)
    path = run / "evidence" / "trades.parquet"
    manifest = run / "manifest.json"
    source = {
        "run_dir": run.name,
        "trades_parquet_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest.exists()
        else None,
        "bar_minutes": BAR_MINUTES,
    }
    return build_evidence(pd.read_parquet(path), bars, source)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import pickle
    import sys

    p = argparse.ArgumentParser(description="Market benchmark for a recorded run (read only)")
    p.add_argument("run_dir")
    p.add_argument("--bars", required=True, help="pickle of {symbol: DataFrame} 5-minute bars")
    p.add_argument("--out", required=True, help="where to write market_benchmark.json")
    args = p.parse_args(argv)
    with open(args.bars, "rb") as fh:
        bars = pickle.load(fh)  # noqa: S301 - a file this workflow wrote
    evidence = evidence_for_run(args.run_dir, bars)
    problems = validate_evidence(evidence)
    Path(args.out).write_text(json.dumps(evidence, indent=1, allow_nan=False))
    sys.stdout.write(f"wrote {args.out}; problems: {problems or 'none'}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
