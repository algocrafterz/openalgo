"""Does the bar data support the conclusion drawn from it?

A backtest cannot be more trustworthy than its input, and bad bars do not announce
themselves - they produce a plausible-looking equity curve. Every check here is one
that has already been found firing against real data in this repo:

  SESSION COMPLETENESS  yfinance drops the last one to three 5-minute bars of 27% of
                        NSE sessions and truncates every 1-minute session at 15:15.
                        A strategy that exits on the close is then exiting somewhere
                        else, on a quarter of its trades.

  OUT-OF-HOURS BARS     Historify held 159,832 SBIN bars stamped between 00:00 and
                        23:59 from a re-run download job. A session anchor computed as
                        "the first bar of the day" landed on a 00:00 bar.

  IMPOSSIBLE BARS       Bars whose high does not bracket the open and close exist in
                        both sources. They trigger stops that never happened.

  CORPORATE ACTIONS     Historify stores RAW broker prices. A 1:10 split is a -90%
                        overnight move, and any strategy reading the previous close
                        trades it.

  STALENESS             A store six months behind still backtests happily, and reports
                        a period nobody asked about.

  CROSS-SOURCE          Two independent feeds that disagree on a day's close mean at
                        least one is wrong. This is the only check that can catch a
                        systematically wrong price, as opposed to a missing one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Full NSE equity session at each interval: 09:15 through 15:29 inclusive.
_SESSION_MINUTES = 375
EXPECTED_BARS = {"1m": 375, "3m": 125, "5m": 75, "10m": 38, "15m": 25, "30m": 13, "1h": 7}

#: A session with fewer than this share of its bars is treated as damaged rather than
#: merely short. Set where a missing opening range or a missing close would be caught.
MIN_SESSION_COMPLETENESS = 0.98

#: Close-to-open moves beyond these bounds are corporate actions, not gaps.
SPLIT_LO, SPLIT_HI = 0.75, 1.33


def expected_bars(interval: str) -> int:
    if interval in EXPECTED_BARS:
        return EXPECTED_BARS[interval]
    n = int("".join(ch for ch in interval if ch.isdigit()) or 1)
    return int(np.ceil(_SESSION_MINUTES / n))


def audit_frame(df: pd.DataFrame, interval: str, symbol: str = "") -> dict:
    """Every defect that matters, for one symbol's bars, as one row."""
    exp = expected_bars(interval)
    idx = df.index
    day = pd.Series(idx.date, index=idx)
    per_day = df.groupby(day.to_numpy()).size()
    o, h, lo_, c = df["Open"], df["High"], df["Low"], df["Close"]

    impossible = ((h < lo_) | (h < o) | (h < c) | (lo_ > o) | (lo_ > c) | (lo_ <= 0)).sum()
    if idx.tz is not None:
        t = pd.Series(idx.time, index=idx)
        outside = int(((t < pd.Timestamp("09:15").time()) |
                       (t > pd.Timestamp("15:29").time())).sum())
    else:
        outside = 0

    daily_close = c.resample("1D").last().dropna()
    daily_open = o.resample("1D").first().dropna()
    ratio = (daily_open / daily_close.shift(1)).dropna()
    splits = int(((ratio < SPLIT_LO) | (ratio > SPLIT_HI)).sum())

    complete = per_day >= exp * MIN_SESSION_COMPLETENESS
    last_day = idx[-1].date()
    today = pd.Timestamp.now(tz=idx.tz).date() if idx.tz else pd.Timestamp.now().date()

    return {
        "symbol": symbol.replace(".NS", ""),
        "bars": len(df),
        "sessions": int(per_day.size),
        "first": str(idx[0].date()),
        "last": str(last_day),
        "stale_days": (today - last_day).days,
        "complete_pct": round(100 * float(complete.mean()), 1),
        "median_bars": int(per_day.median()),
        "impossible": int(impossible),
        "dup_ts": int(idx.duplicated().sum()),
        "outside_hours": outside,
        "zero_vol_pct": round(100 * float((df["Volume"] <= 0).mean()), 2)
        if "Volume" in df else np.nan,
        "nan_ohlc": int(df[["Open", "High", "Low", "Close"]].isna().sum().sum()),
        "split_days": splits,
    }


def audit(frames: dict[str, pd.DataFrame], interval: str) -> pd.DataFrame:
    """Per-symbol audit of a loaded universe."""
    return pd.DataFrame([audit_frame(df, interval, s) for s, df in frames.items()])


def verdict(report: pd.DataFrame, interval: str, min_sessions: int = 250) -> pd.DataFrame:
    """Which symbols are fit to report from, and why the rest are not.

    Separate from `audit` on purpose: the audit states facts, this applies a policy,
    and a policy is the kind of thing that should be visible and arguable rather than
    buried inside a loader.
    """
    r = report.copy()
    reasons = []
    for row in r.itertuples():
        why = []
        if row.sessions < min_sessions:
            why.append(f"only {row.sessions} sessions")
        if row.complete_pct < 90:
            why.append(f"{100 - row.complete_pct:.0f}% of sessions missing bars")
        if row.impossible:
            why.append(f"{row.impossible} impossible bars")
        if row.outside_hours:
            why.append(f"{row.outside_hours} bars outside market hours")
        if row.dup_ts:
            why.append(f"{row.dup_ts} duplicate timestamps")
        if row.stale_days > 10:
            why.append(f"{row.stale_days} days stale")
        reasons.append("; ".join(why) or "ok")
    r["usable"] = [x == "ok" for x in reasons]
    r["why_not"] = reasons
    return r[["symbol", "sessions", "complete_pct", "stale_days", "usable", "why_not"]]


def cross_source(historify: dict[str, pd.DataFrame], yahoo: dict[str, pd.DataFrame],
                 tol_bps: float = 25.0) -> pd.DataFrame:
    """Do two independent feeds agree on the same day's close?

    The only check in this file that can catch a price that is WRONG rather than
    missing. Compares daily closes over the overlapping dates; a symbol that disagrees
    on more than a handful of days has a real problem in one of the two sources -
    usually an unadjusted corporate action on one side.
    """
    rows = []
    for sym, hdf in historify.items():
        ydf = yahoo.get(sym) or yahoo.get(f"{sym}.NS")
        if ydf is None:
            continue
        a = hdf["Close"].resample("1D").last().dropna()
        b = ydf["Close"].resample("1D").last().dropna()
        a.index, b.index = a.index.date, b.index.date
        j = pd.concat([a.rename("h"), b.rename("y")], axis=1, join="inner").dropna()
        if j.empty:
            continue
        diff_bps = (j.h / j.y - 1.0).abs() * 10_000
        rows.append({
            "symbol": sym, "overlap_days": len(j),
            "median_bps": round(float(diff_bps.median()), 1),
            "p95_bps": round(float(diff_bps.quantile(0.95)), 1),
            "days_over_tol": int((diff_bps > tol_bps).sum()),
            "agrees": bool(diff_bps.median() <= tol_bps),
        })
    return pd.DataFrame(rows).sort_values("median_bps", ascending=False)


def main() -> None:
    import argparse

    from signal_engine.backtest import data

    ap = argparse.ArgumentParser(prog="signal_engine.backtest.dataquality")
    ap.add_argument("--source", default="historify", choices=["historify", "yahoo"])
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--period", default="60d", help="yahoo only")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--cross-check", action="store_true",
                    help="also compare Historify against Yahoo on daily closes")
    args = ap.parse_args()

    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 300)
    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    if args.source == "historify":
        frames = data.from_historify(symbols=syms, interval=args.interval,
                                     min_sessions=1, drop_split_days=False)
    else:
        frames = data.load(symbols=[f"{s}.NS" for s in syms] if syms else None,
                           period=args.period, interval=args.interval, min_bars=1)

    rep = audit(frames, args.interval)
    print(f"=== {args.source} @ {args.interval}: {len(frames)} symbols ===")
    print(rep.to_string(index=False))
    print("\n=== totals ===")
    print(f"bars {rep.bars.sum():,} | sessions/symbol median {int(rep.sessions.median())} | "
          f"session completeness median {rep.complete_pct.median():.1f}% | "
          f"impossible bars {rep.impossible.sum()} | out-of-hours {rep.outside_hours.sum()} | "
          f"duplicate timestamps {rep.dup_ts.sum()}")
    print("\n=== fitness ===")
    v = verdict(rep, args.interval)
    print(f"{int(v.usable.sum())}/{len(v)} usable")
    print(v[~v.usable].to_string(index=False) if (~v.usable).any() else "all symbols pass")

    if args.cross_check:
        print("\n=== Historify vs Yahoo, daily closes ===")
        y = data.load(symbols=[f"{s}.NS" for s in frames], period="2y",
                      interval="1d", min_bars=1)
        y = {k.replace(".NS", ""): v for k, v in y.items()}
        print(cross_source(frames, y).to_string(index=False))


if __name__ == "__main__":
    main()
