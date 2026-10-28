"""Refine the best IB% definition family (read-only; run fit_definition.py first).

    uv run python <this dir>/fit_refine.py

At the 12:01 poll: IB end time 10:00 to 10:45 and the averaging of prior daily ranges (simple
mean of 3 to 8 sessions, 5-session median, EMA span and Wilder 3 to 10), then the signed
residual and the narrow/mid/wide agreement of the winner with the site.
"""

import datetime as dt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[5]
ENDS = (600, 615, 620, 625, 630, 645)  # IB end, minutes after midnight IST


def bars(symbols):
    h = duckdb.connect(f"{ROOT}/db/historify.duckdb", read_only=True)
    h.register("syms", pd.DataFrame({"symbol": symbols}))
    windows = ",".join(
        f"max(high) filter (where m >= 555 and m < {e}) as h{e}, "
        f"min(low) filter (where m >= 555 and m < {e}) as l{e}"
        for e in ENDS
    )
    q = (
        "with t as (select symbol, cast(to_timestamp(timestamp + 19800) as date) as d, "
        "((timestamp + 19800) % 86400) / 60 as m, high, low from market_data "
        "where exchange = 'NSE' and interval = '1m' and timestamp >= ? "
        "and symbol in (select symbol from syms)) "
        f"select symbol, d, max(high) as h, min(low) as l, {windows} "
        "from t where m >= 555 and m < 930 group by 1, 2"
    )
    d = h.execute(q, [int(dt.datetime(2026, 6, 1).timestamp())]).df()
    d = d.sort_values(["symbol", "d"])
    d["day"] = d.d.astype(str)
    d["rng"] = d.h - d.l
    return d


def errors(site, model):
    err = (model - site).abs()
    ok = err.notna()
    return {
        "mae": err.median(),
        "within5": (err <= 5).mean(),
        "within1": (err <= 1).mean(),
        "corr": np.corrcoef(model[ok], site[ok])[0, 1],
    }


def tercile(s):
    return pd.qcut(s, 3, labels=["narrow", "mid", "wide"])


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    m = pd.read_pickle(HERE / "results" / "fit_frame.pkl")
    m = m[m["at"] == "12:01:00"]
    d = bars(sorted(m.symbol.unique()))
    g = d.groupby("symbol").rng
    dens = {
        f"adr{n}": g.transform(lambda x, n=n: x.rolling(n, min_periods=n).mean().shift(1))
        for n in range(3, 9)
    }
    dens["med5"] = g.transform(lambda x: x.rolling(5, min_periods=5).median().shift(1))
    for n in range(3, 11):
        dens[f"ema_span{n}"] = g.transform(
            lambda x, n=n: x.ewm(span=n, adjust=False, min_periods=n).mean().shift(1)
        )
        dens[f"wilder{n}"] = g.transform(
            lambda x, n=n: x.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().shift(1)
        )
    d = d.assign(**dens)
    x = m[["day", "symbol", "ib_pct"]].merge(d, on=["day", "symbol"])
    rows = []
    for e in ENDS:
        for den in dens:
            model = 100 * (x[f"h{e}"] - x[f"l{e}"]) / x[den]
            rows.append({"ib_end_min": e, "den": den, **errors(x.ib_pct, model)})
    print(pd.DataFrame(rows).sort_values("mae").head(12).round(3).to_string(index=False))

    best = 100 * (x.h615 - x.l615) / x.adr6
    ok = best.notna()
    print(
        "adr6 signed residual quantiles",
        (x.ib_pct - best).quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(2).tolist(),
    )
    print("rank corr", round(x.ib_pct.corr(best, method="spearman"), 3))
    a, b = tercile(x.ib_pct[ok]), tercile(best[ok])
    print("tercile agreement", round((a == b).mean(), 3))
    print(pd.crosstab(a, b).to_string())
    for cut in (30, 90):
        same = ((x.ib_pct[ok] < cut) == (best[ok] < cut)).mean()
        print(f"same side of {cut}: {same:.3f}")
