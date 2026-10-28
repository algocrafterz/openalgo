"""Which Market Profile states, read EARLY from the BreakingTrade snapshots, predict the rest of
the day? (read-only research)

    uv run python <this dir>/state_study.py

Universe: every stock in breakingtrade.db market_profile snapshots with Historify 1m bars.
At each decision time T the stock's state is its last snapshot at or before T that day; entry is
the 1m open at the minute after T, exits are the 1m close at T+61 min and at 14:45. "Excess" is
the stock's return minus the equal-weight mean of the universe over the same minutes, signed by
the direction the state implies (fixed in DIRECTIONS before any result was seen). t is
clustered by day: the daily mean of signed excess, t over days. Periods: P1 2026-08-18..09-23,
P2 2026-09-24..10-27. Writes results/states_<T>.csv (one row per stock-day).
"""

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "2026-10-amt-replay"))
import bt_replay as R  # noqa: E402

OUT = HERE / "results"
OUT.mkdir(exist_ok=True)
DB = sqlite3.connect(f"file:{R.ROOT}/signal_engine/data/breakingtrade.db?mode=ro", uri=True)
TIMES = ("09:35", "10:20", "11:01")
P2_START = "2026-09-24"
END = dt.time(14, 45)
ANCHOR = dt.date(2000, 1, 1)  # only for time arithmetic

# AMT-implied direction of each state: +1 up, -1 down. Fixed before running.
DIRECTIONS = {
    "opening": {"above_prior_vah": 1, "gap_up": 1, "below_prior_val": -1, "gap_down": -1},
    "tail": {"buy_tail": 1, "sell_tail": -1},
    "single_print": {"failed_low": 1, "failed_high": -1},
    "poor_hl": {"poor_high": 1, "poor_low": -1},
    "tpo_pos": {"tpo_ext_high": 1, "near_day_high": 1, "above_va": 1, "near_va_high": 1,
                "tpo_ext_low": -1, "near_day_low": -1, "below_va": -1, "near_va_low": -1},
    "tpo_pos_prev": {"above_pdh": 1, "below_pdl": -1},
}
DIRECTED = ("open_type", "day_type")  # direction comes from <col>_dir


def snapshots(t):
    rows = DB.execute(
        "select date(captured_at) d, symbol, max(captured_at), opening, open_type, open_type_dir, "
        "tail, single_print, poor_hl, day_type, day_type_dir, tpo_pos, tpo_pos_prev, ib_pct "
        "from snapshots where kind='market_profile' and captured_at >= '2026-08-18' "
        "and time(captured_at) <= ? and time(captured_at) >= time(?, '-10 minutes') "
        "group by d, symbol", (t + ":00", t + ":00")).fetchall()
    cols = ["day", "sym", "at", "opening", "open_type", "open_type_dir", "tail", "single_print",
            "poor_hl", "day_type", "day_type_dir", "tpo_pos", "tpo_pos_prev", "ib_pct"]
    return pd.DataFrame(rows, columns=cols)


def day_matrix(day):
    a = int(dt.datetime.combine(day, dt.time(9, 15), R.IST).timestamp())
    b = int(dt.datetime.combine(day, dt.time(15, 30), R.IST).timestamp())
    df = R.H.execute("select symbol, timestamp, open, close from market_data where exchange='NSE' "
                     "and interval='1m' and timestamp between ? and ?", [a, b]).df()
    if df.empty:
        return None, None
    df["t"] = pd.to_datetime(df.timestamp, unit="s", utc=True).dt.tz_convert(R.IST).dt.time
    return (df.pivot_table(index="t", columns="symbol", values="open"),
            df.pivot_table(index="t", columns="symbol", values="close"))


def first_at(frame, t):
    idx = frame.index[frame.index >= t]
    return idx[0] if len(idx) else None


def plus(t, minutes):
    return (dt.datetime.combine(ANCHOR, t) + dt.timedelta(minutes=minutes)).time()


def returns(snap, t):
    out = []
    entry_t = plus(dt.time.fromisoformat(t), 1)
    for day, g in snap.groupby("day"):
        opens, closes = day_matrix(dt.date.fromisoformat(day))
        if opens is None:
            continue
        t0, t60, t_end = first_at(opens, entry_t), first_at(closes, plus(entry_t, 60)), first_at(closes, END)
        if None in (t0, t60, t_end):
            continue
        p0 = opens.loc[t0]
        for name, t1 in (("r60", t60), ("rclose", t_end)):
            r = closes.loc[t1] / p0 - 1
            g = g.assign(**{name: g.sym.map((r - r.mean()) * 1e4)})
        out.append(g)
    return pd.concat(out).dropna(subset=["rclose"])


def signed_tests(df):
    """One row per (feature, value): signed excess by period, day-clustered t."""
    rows = []
    feats = [(c, v, s) for c, m in DIRECTIONS.items() for v, s in m.items()]
    feats += [(c, v, None) for c in DIRECTED for v in df[c].dropna().unique()]
    for col, val, sign in feats:
        sub = df[df[col] == val]
        if sign is None:
            s = sub[col + "_dir"].map({"up": 1, "down": -1})
            sub, s = sub[s.notna()], s.dropna()
        else:
            s = pd.Series(sign, index=sub.index)
        if len(sub) < 40:
            continue
        rec = {"feature": f"{col}={val}" + ("" if sign is not None else " (by dir)"), "n": len(sub)}
        for per, mask in (("P1", sub.day < P2_START), ("P2", sub.day >= P2_START)):
            rec[f"{per}_n"] = int(mask.sum())
            for h in ("r60", "rclose"):
                x = (sub[h] * s)[mask]
                daily = x.groupby(sub.day[mask]).mean()
                rec[f"{per}_{h}"] = x.mean()
                rec[f"{per}_{h}_t"] = (daily.mean() / (daily.std() / np.sqrt(len(daily)))
                                       if len(daily) > 2 else np.nan)
        rows.append(rec)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    for t in TIMES:
        df = returns(snapshots(t), t)
        df.to_csv(OUT / f"states_{t.replace(':', '')}.csv", index=False)
        res = signed_tests(df).round(2).sort_values("P2_rclose", ascending=False)
        print(f"\n===== decision {t}: {len(df)} stock-days, {df.day.nunique()} days "
              f"(signed excess bps; r60 = next 60 min, rclose = to 14:45)")
        print(res[["feature", "P1_n", "P1_r60", "P1_r60_t", "P1_rclose", "P1_rclose_t",
                   "P2_n", "P2_r60", "P2_r60_t", "P2_rclose", "P2_rclose_t"]].to_string(index=False))
