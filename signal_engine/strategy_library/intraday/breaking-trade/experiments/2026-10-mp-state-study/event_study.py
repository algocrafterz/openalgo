"""Act the moment a label or a guide scan first appears: is there anything left? (read-only)

    uv run python <this dir>/event_study.py

Each event is the FIRST poll of the day (inside its window) at which a stock's market_profile
snapshot meets the condition. Entry is the 1m open of the minute after that poll (the earliest our
poller lets us act; the site itself refreshes every 2 minutes). Signed excess vs the equal-weight
universe from entry to +15/+30/+60 min and 14:45, plus the same to 14:45 entered 10 and 20 minutes
later (how fast any edge decays, i.e. whether faster polling would help). Direction per event is
the guide's own instruction (https://breakingtrade.com/scanner-guide, fetched 2026-10-28).
t is clustered by day. Periods P1 2026-08-18..09-23, P2 2026-09-24..10-27.
"""

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state_study as S  # noqa: E402

T = dt.time.fromisoformat


def _count(df):
    return pd.to_numeric(df.tpo_pos_count, errors="coerce").fillna(0)


# name: (window start, window end, condition(df) -> bool Series, direction +1/-1 or "dir")
EVENTS = {
    "label buy_tail -> long": ("09:20", "13:00", lambda d: d["tail"] == "buy_tail", 1),
    "label sell_tail -> short": ("09:20", "13:00", lambda d: d["tail"] == "sell_tail", -1),
    "label failed_low -> long": ("09:20", "13:00", lambda d: d.single_print == "failed_low", 1),
    "label failed_high -> short": ("09:20", "13:00", lambda d: d.single_print == "failed_high", -1),
    "label poor_high -> toward it (long)": ("09:20", "13:00", lambda d: d.poor_hl == "poor_high", 1),
    "label poor_low -> toward it (short)": ("09:20", "13:00", lambda d: d.poor_hl == "poor_low", -1),
    "label open_drive -> its direction": ("09:20", "10:30", lambda d: d.open_type == "open_drive", "dir"),
    "live print up 3-5 TPO": ("09:20", "13:00",
                              lambda d: (d.tpo_pos == "tpo_ext_high") & _count(d).between(3, 5), 1),
    "live print up >=6 TPO": ("09:20", "13:00",
                              lambda d: (d.tpo_pos == "tpo_ext_high") & (_count(d) >= 6), 1),
    "live print down 3-5 TPO": ("09:20", "13:00",
                                lambda d: (d.tpo_pos == "tpo_ext_low") & _count(d).between(3, 5), -1),
    "live print down >=6 TPO": ("09:20", "13:00",
                                lambda d: (d.tpo_pos == "tpo_ext_low") & (_count(d) >= 6), -1),
    "scan10 live print up + Trend up": (
        "09:20", "13:00",
        lambda d: (d.tpo_pos == "tpo_ext_high") & (_count(d) >= 3) & (d.day_type == "Trend")
        & (d.day_type_dir == "up"), 1),
    "scan10 live print down + Trend down": (
        "09:20", "13:00",
        lambda d: (d.tpo_pos == "tpo_ext_low") & (_count(d) >= 3) & (d.day_type == "Trend")
        & (d.day_type_dir == "down"), -1),
    "scan6 sell tail fade (short)": (
        "10:00", "14:00",
        lambda d: (d["tail"] == "sell_tail") & (d.day_type == "Normal Var") & (d.day_type_dir == "down")
        & (d.tpo_pos == "in_va"), -1),
    "scan6 mirror buy tail (long)": (
        "10:00", "14:00",
        lambda d: (d["tail"] == "buy_tail") & (d.day_type == "Normal Var") & (d.day_type_dir == "up")
        & (d.tpo_pos == "in_va"), 1),
    "scan8 poor high magnet (long)": (
        "12:30", "15:00",
        lambda d: (d.poor_hl == "poor_high") & (d.tpo_pos == "in_va") & (d.day_type == "Neutral Center"), 1),
    "scan8 mirror poor low (short)": (
        "12:30", "15:00",
        lambda d: (d.poor_hl == "poor_low") & (d.tpo_pos == "in_va") & (d.day_type == "Neutral Center"), -1),
    "scan11 range-day edge fade, above VA (short)": (
        "11:00", "14:30", lambda d: (d.day_type == "Normal") & (d.tpo_pos == "above_va"), -1),
    "scan11 range-day edge fade, below VA (long)": (
        "11:00", "14:30", lambda d: (d.day_type == "Normal") & (d.tpo_pos == "below_va"), 1),
}


def load():
    rows = S.DB.execute(
        "select date(captured_at), time(captured_at), symbol, opening, open_type, open_type_dir, tail, "
        "single_print, poor_hl, day_type, day_type_dir, tpo_pos, tpo_pos_count from snapshots "
        "where kind='market_profile' and captured_at >= '2026-08-18'").fetchall()
    cols = ["day", "at", "sym", "opening", "open_type", "open_type_dir", "tail", "single_print",
            "poor_hl", "day_type", "day_type_dir", "tpo_pos", "tpo_pos_count"]
    return pd.DataFrame(rows, columns=cols)


def first_events(snap):
    out = []
    for name, (lo, hi, cond, direction) in EVENTS.items():
        w = snap[(snap["at"] >= lo + ":00") & (snap["at"] <= hi + ":00")]
        hit = w[cond(w)].sort_values("at").groupby(["day", "sym"]).head(1)
        if direction == "dir":
            sign = hit.open_type_dir.map({"up": 1, "down": -1})
        else:
            sign = pd.Series(direction, index=hit.index)
        hit = hit.assign(event=name, sign=sign).dropna(subset=["sign"])
        out.append(hit[["day", "at", "sym", "event", "sign"]])
    return pd.concat(out)


def measure(events):
    rows = []
    for day, g in events.groupby("day"):
        opens, closes = S.day_matrix(dt.date.fromisoformat(day))
        if opens is None:
            continue
        day_open = opens.iloc[0]
        for e in g.itertuples():
            entry = S.plus(T(e.at), 1)
            if entry >= S.END or e.sym not in opens.columns:
                continue
            rec = {"day": day, "sym": e.sym, "event": e.event, "at": e.at}
            for lag in (0, 10, 20):
                t0 = S.first_at(opens, S.plus(entry, lag))
                if t0 is None or t0 >= S.END:
                    continue
                p0 = opens.loc[t0]
                if lag == 0:
                    before = (p0 / day_open - 1) - (p0 / day_open - 1).mean()
                    rec["before"] = e.sign * before.get(e.sym, np.nan) * 1e4
                    horizons = [(f"r{h}", S.plus(t0, h)) for h in (15, 30, 60)] + [("rclose", S.END)]
                else:
                    horizons = [(f"rclose_lag{lag}", S.END)]
                for name, t in horizons:
                    t1 = S.first_at(closes, min(t, S.END))
                    if t1 is None:
                        continue
                    r = closes.loc[t1] / p0 - 1
                    rec[name] = e.sign * ((r - r.mean()).get(e.sym, np.nan)) * 1e4
            rows.append(rec)
    return pd.DataFrame(rows)


def stat(sub, col):
    x = sub[col].dropna()
    daily = x.groupby(sub.loc[x.index, "day"]).mean()
    t = daily.mean() / (daily.std() / np.sqrt(len(daily))) if len(daily) > 2 else np.nan
    return f"{x.mean():+6.1f}({t:+.1f})"


if __name__ == "__main__":
    d = measure(first_events(load()))
    d.to_csv(S.OUT / "events.csv", index=False)
    cols = ["r15", "r30", "r60", "rclose", "rclose_lag10", "rclose_lag20"]
    print("signed excess bps (day-clustered t); before = move already made since the open")
    print(f"{'event':46} {'per':3} {'n':>5} {'before':>7}  " + "  ".join(f"{c:>12}" for c in cols))
    for name in EVENTS:
        for per, m in (("P1", d.day < S.P2_START), ("P2", d.day >= S.P2_START)):
            sub = d[(d.event == name) & m]
            if len(sub) < 15:
                print(f"{name:46} {per:3} {len(sub):5d}  (too few)")
                continue
            print(f"{name:46} {per:3} {len(sub):5d} {sub.before.mean():+7.1f}  "
                  + "  ".join(f"{stat(sub, c):>12}" for c in cols))
