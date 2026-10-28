"""Which definition reproduces the BreakingTrade website's IB%? (read-only)

    uv run python <this dir>/fit_definition.py

For every market_profile snapshot taken at 10:31 or 12:01 IST (after the IB is final; 10:31 is
the first poll past the site's own 2-minute refresh lag) compute candidate IB% values from
Historify 1m bars and compare with snapshots.ib_pct. Numerators: the IB range 09:15-10:15, and
the session range up to the snapshot (to tell "IB" from "range so far" on days that extended).
Denominators use only completed prior sessions. Writes results/fit.csv.
"""

import datetime as dt
import sqlite3
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[5]
OUT = HERE / "results"
NS = (5, 10, 14, 20)
POLLS = ("10:31:00", "12:01:00")


def snapshots():
    db = sqlite3.connect(
        f"file:{ROOT}/signal_engine/data/breakingtrade.db?mode=ro", uri=True, timeout=30
    )
    q = (
        "select date(captured_at) day, time(captured_at) at, symbol, ib_pct from snapshots "
        "where kind='market_profile' and captured_at >= '2026-08-18' and ib_pct is not null "
        f"and time(captured_at) in {POLLS}"
    )
    s = pd.read_sql(q, db)
    s["ib_pct"] = pd.to_numeric(s.ib_pct, errors="coerce")
    return s.dropna()


def daily_from_1m(symbols):
    """Per symbol and IST day: high, low, close, IB high/low, and range to 10:31 / 12:01."""
    h = duckdb.connect(f"{ROOT}/db/historify.duckdb", read_only=True)
    start = int(dt.datetime(2026, 6, 1).timestamp())
    h.register("syms", pd.DataFrame({"symbol": sorted(symbols)}))
    return h.execute(
        """
        with b as (
          select symbol, timestamp + 19800 as ts, high, low, close from market_data
          where exchange = 'NSE' and interval = '1m' and timestamp >= ?
            and symbol in (select symbol from syms)
        ), t as (
          select symbol, cast(to_timestamp(ts) as date) as day,
                 (ts % 86400) / 60 as m, high, low, close, ts from b
        )
        select symbol, day,
          max(high) h, min(low) l, arg_max(close, ts) c,
          max(high) filter (where m >= 555 and m < 615) ibh,
          min(low)  filter (where m >= 555 and m < 615) ibl,
          max(high) filter (where m >= 555 and m < 631) h1031,
          min(low)  filter (where m >= 555 and m < 631) l1031,
          max(high) filter (where m >= 555 and m < 721) h1201,
          min(low)  filter (where m >= 555 and m < 721) l1201,
          count(*) filter (where m >= 555 and m < 615) ib_bars
        from t where m >= 555 and m < 930 group by symbol, day order by symbol, day
        """,
        [start],
    ).df()


def denominators(d):
    d = d.sort_values(["symbol", "day"]).copy()
    g = d.groupby("symbol")
    d["rng"] = d.h - d.l
    d["ibr"] = d.ibh - d.ibl
    pc = g.c.shift(1)
    d["tr"] = np.maximum(d.h, pc) - np.minimum(d.l, pc)
    d["tr"] = d.tr.fillna(d.rng)
    out = {"prev_rng": g.rng.shift(1)}
    for n in NS:
        out[f"adr{n}"] = g.rng.transform(lambda x, n=n: x.rolling(n, min_periods=n).mean().shift(1))
        out[f"satr{n}"] = g.tr.transform(lambda x, n=n: x.rolling(n, min_periods=n).mean().shift(1))
        out[f"watr{n}"] = g.tr.transform(
            lambda x, n=n: x.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().shift(1)
        )
        out[f"aibr{n}"] = g.ibr.transform(
            lambda x, n=n: x.rolling(n, min_periods=n).mean().shift(1)
        )
    return d.assign(**out)


def score(site, model):
    e = (model - site).abs()
    ok = e.notna()
    return {
        "n": int(ok.sum()),
        "median_abs_err": e[ok].median(),
        "p90_abs_err": e[ok].quantile(0.9),
        "within_5": (e[ok] <= 5).mean(),
        "corr": np.corrcoef(site[ok], model[ok])[0, 1],
        "median_ratio": (site[ok] / model[ok]).median(),
    }


if __name__ == "__main__":
    s = snapshots()
    d = denominators(daily_from_1m(s.symbol.unique()))
    d["day"] = d.day.astype(str)
    m = s.merge(d, left_on=["day", "symbol"], right_on=["day", "symbol"], how="inner")
    m = m[m.ib_bars >= 55]
    m["sofar"] = np.where(m["at"] == "10:31:00", m.h1031 - m.l1031, m.h1201 - m.l1201)
    dens = ["prev_rng"] + [f"{k}{n}" for k in ("adr", "satr", "watr", "aibr") for n in NS]
    rows = []
    for at in POLLS:
        sub = m[m["at"] == at]
        for num in ("ibr", "sofar"):
            for den in dens:
                rows.append(
                    {
                        "poll": at,
                        "num": num,
                        "den": den,
                        **score(sub.ib_pct, 100 * sub[num] / sub[den]),
                    }
                )
        rows.append(
            {"poll": at, "num": "ibr", "den": "price", **score(sub.ib_pct, 100 * sub.ibr / sub.c)}
        )
    res = pd.DataFrame(rows).sort_values(["poll", "median_abs_err"])
    res.to_csv(OUT / "fit.csv", index=False)
    pd.set_option("display.width", 200)
    for at in POLLS:
        print(res[res.poll == at].head(12).round(3).to_string(index=False))
    m.to_pickle(OUT / "fit_frame.pkl")
