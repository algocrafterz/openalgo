"""Does the scanner's pick carry direction beyond the market? (read-only)

    uv run python <this dir>/selection_check.py [FROM TO]

For every new scan pick (breakingtrade.db scan_hits), the signed return in the pick's
direction from the first scanner poll at or after the pick (1m open) to +30 min, +60 min,
+120 min and 14:45, against the same-direction return of the average F&O stock in Historify
over the same minutes. Also the move already made from the day's open to the pick. No entry,
stop or target rule is involved: this measures the selection alone. Writes
results/selection_<FROM>.csv.
"""

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import amt_replay as A  # noqa: E402
import bt_replay as R  # noqa: E402

HORIZONS = (30, 60, 120)
FROM, TO = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2026-08-18", "2099-01-01")


def day_matrix(day):
    """Universe 1m closes and opens for one day, indexed by minute, one column per symbol."""
    a = int(dt.datetime.combine(day, dt.time(9, 15), R.IST).timestamp())
    b = int(dt.datetime.combine(day, dt.time(15, 30), R.IST).timestamp())
    df = R.H.execute(
        "select symbol, timestamp, open, close from market_data where exchange='NSE' "
        "and interval='1m' and timestamp between ? and ?", [a, b]).df()
    if df.empty:
        return None, None
    df["t"] = pd.to_datetime(df.timestamp, unit="s", utc=True).dt.tz_convert(R.IST).dt.time
    return (df.pivot_table(index="t", columns="symbol", values="close"),
            df.pivot_table(index="t", columns="symbol", values="open"))


def at_or_after(frame, t):
    rows = frame.index[frame.index >= t]
    return rows[0] if len(rows) else None


def main():
    raw = A.B.execute(
        "select symbol, scan, direction, min(captured_at) from scan_hits where is_new=1 "
        "and captured_at >= ? and captured_at < ? group by symbol, date(captured_at), scan "
        "order by 4", (FROM, TO)).fetchall()
    by_day = {}
    for sym, scan, d, t in raw:
        seen = dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        by_day.setdefault(seen.date(), []).append((sym, scan, 1 if d == "up" else -1, seen))
    out = []
    for day, items in sorted(by_day.items()):
        closes, opens = day_matrix(day)
        if closes is None:
            continue
        polls = A.polls(day)
        end_t = at_or_after(closes, dt.time(14, 45))
        day_open = opens.iloc[0]
        for sym, scan, s, seen in items:
            poll = next((p for p in polls if p >= seen), None)
            if poll is None or sym not in closes.columns:
                continue
            t0 = at_or_after(opens, poll.time())
            if t0 is None or end_t is None or t0 >= end_t:
                continue
            p0 = opens.at[t0, sym]
            if pd.isna(p0) or pd.isna(day_open[sym]):
                continue
            mkt0 = opens.loc[t0]
            rec = {"day": str(day), "sym": sym, "scan": scan, "side": "LONG" if s > 0 else "SHORT",
                       "poll": poll.strftime("%H:%M"),
                       "before_bps": s * (p0 / day_open[sym] - 1) * 1e4}
            marks = [(f"{h}m", (dt.datetime.combine(day, t0) + dt.timedelta(minutes=h)).time())
                     for h in HORIZONS] + [("close", dt.time(14, 45))]
            for name, t in marks:
                t1 = at_or_after(closes, min(t, end_t))
                if t1 is None or pd.isna(closes.at[t1, sym]):
                    continue
                own = s * (closes.at[t1, sym] / p0 - 1) * 1e4
                mkt = s * (closes.loc[t1] / mkt0 - 1).mean() * 1e4
                rec[f"own_{name}"] = own
                rec[f"excess_{name}"] = own - mkt
            out.append(rec)
    d = pd.DataFrame(out)
    d.to_csv(f"{R.OUT}/selection_{FROM}.csv", index=False)
    cols = [c for c in d.columns if c.startswith(("own_", "excess_"))]

    def show(label, sub):
        print(f"\n{label}  n={len(sub)}  move before pick {sub.before_bps.mean():+.1f} bps")
        for c in cols:
            m = sub[c].mean()
            t = m / (sub[c].std() / sub[c].count() ** 0.5)
            print(f"  {c:14s} {m:+7.1f} bps  t={t:+.2f}")

    print(f"Selection check {FROM}..{TO}: {len(d)} picks on {d.day.nunique()} days")
    show("ALL", d)
    for side, sub in d.groupby("side"):
        show(side, sub)
    for scan, sub in d.groupby("scan"):
        if len(sub) >= 20:
            show(f"scan: {scan}", sub)


if __name__ == "__main__":
    main()
