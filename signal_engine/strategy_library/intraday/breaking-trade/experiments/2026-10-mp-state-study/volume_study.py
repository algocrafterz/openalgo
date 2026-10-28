"""Does participation decide whether the morning's move continues? (read-only research)

    uv run python <this dir>/volume_study.py

At T, each stock's move since the open (Historify) sets its direction; its pace-adjusted volume
surge (breakingtrade.db volume snapshot surge_x at or before T, adjusted by scans.pace_adjusted_surge)
is split into terciles per day. Signed excess return from T+1 min to 14:45 and over 60 min, as in
state_study.py. Pre-declared AMT reading: high participation continues, low participation fades.
"""

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import state_study as S  # noqa: E402

from signal_engine.analysis.breakingtrade.scans import pace_adjusted_surge  # noqa: E402

for t in ("10:20", "11:01"):
    rows = S.DB.execute(
        "select date(captured_at), symbol, surge_x from snapshots where kind='volume' "
        "and captured_at >= '2026-08-18' and time(captured_at) <= ? "
        "and time(captured_at) >= time(?, '-15 minutes')", (t + ":00", t + ":00")).fetchall()
    vol = pd.DataFrame(rows, columns=["day", "sym", "surge"]).dropna()
    vol["surge"] = pd.to_numeric(vol.surge, errors="coerce")
    vol = vol.dropna().groupby(["day", "sym"]).surge.last().reset_index()
    at_time = dt.time.fromisoformat(t)
    vol["pace"] = vol.surge.apply(lambda s, at=at_time: pace_adjusted_surge(s, at))
    out = []
    entry_t = S.plus(dt.time.fromisoformat(t), 1)
    for day, g in vol.groupby("day"):
        opens, closes = S.day_matrix(dt.date.fromisoformat(day))
        if opens is None:
            continue
        t0, t60, te = S.first_at(opens, entry_t), S.first_at(closes, S.plus(entry_t, 60)), S.first_at(closes, S.END)
        if None in (t0, t60, te):
            continue
        p0, day_open = opens.loc[t0], opens.iloc[0]
        before = (p0 / day_open - 1) - (p0 / day_open - 1).mean()
        g = g.assign(sign=g.sym.map(np.sign(before)), before=g.sym.map(before * 1e4))
        for name, t1 in (("r60", t60), ("rclose", te)):
            r = closes.loc[t1] / p0 - 1
            g = g.assign(**{name: g.sym.map((r - r.mean()) * 1e4)})
        g["tercile"] = pd.qcut(g.pace.rank(method="first"), 3, labels=["low", "mid", "high"])
        out.append(g)
    d = pd.concat(out).dropna(subset=["rclose", "sign"])
    print(f"\n===== {t}: {len(d)} stock-days, {d.day.nunique()} days. Signed by the move since open (excess bps)")
    for per, m in (("P1", d.day < S.P2_START), ("P2", d.day >= S.P2_START)):
        for terc in ("low", "mid", "high"):
            sub = d[m & (d.tercile == terc)]
            line = f"  {per} {terc:4s} n={len(sub):5d} move before {(sub.before * sub.sign).mean():+6.1f}"
            for h in ("r60", "rclose"):
                x = sub[h] * sub.sign
                daily = x.groupby(sub.day).mean()
                line += f"  {h} {x.mean():+6.1f} (t {daily.mean() / (daily.std() / np.sqrt(len(daily))):+.2f})"
            print(line)
