"""The scanner's picks split by participation at the pick (read-only research).

    uv run python <this dir>/picks_by_volume.py

Joins ../2026-10-amt-replay/results/selection_2026-08-18.csv (selection_check.py) with the
pace-adjusted volume surge at the pick's poll, ranked against the whole universe at that poll
(percentile 0-1). Reports the pick's excess return in its own direction by universe tercile.
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

picks = pd.read_csv(HERE.parent / "2026-10-amt-replay" / "results" / "selection_2026-08-18.csv")
rows = S.DB.execute(
    "select date(captured_at), time(captured_at), symbol, surge_x from snapshots "
    "where kind='volume' and captured_at >= '2026-08-18'").fetchall()
vol = pd.DataFrame(rows, columns=["day", "at", "sym", "surge"])
vol["surge"] = pd.to_numeric(vol.surge, errors="coerce")
vol = vol.dropna()
vol["pace"] = [pace_adjusted_surge(s, dt.time.fromisoformat(a)) for s, a in zip(vol.surge, vol["at"], strict=False)]
vol["pct"] = vol.groupby(["day", "at"]).pace.rank(pct=True)
out = []
for p in picks.itertuples():
    snap = vol[(vol.day == p.day) & (vol.sym == p.sym) & (vol["at"] <= p.poll + ":00")]
    if snap.empty:
        continue
    out.append({**p._asdict(), "vol_pct": snap.sort_values("at").iloc[-1].pct})
d = pd.DataFrame(out)
d["tercile"] = pd.cut(d.vol_pct, [0, 1 / 3, 2 / 3, 1.0], labels=["low", "mid", "high"], include_lowest=True)
print(f"{len(d)} of {len(picks)} picks with a volume reading; share per universe tercile:",
      d.tercile.value_counts(normalize=True).round(2).to_dict())
for per, m in (("P1", d.day < S.P2_START), ("P2", d.day >= S.P2_START), ("ALL", d.day > "")):
    for terc in ("low", "mid", "high"):
        sub = d[m & (d.tercile == terc)]
        if len(sub) < 5:
            continue
        line = f"  {per:3s} {terc:4s} n={len(sub):4d} move before {sub.before_bps.mean():+6.1f}"
        for h in ("excess_60m", "excess_close"):
            daily = sub.groupby("day")[h].mean()
            t = daily.mean() / (daily.std() / np.sqrt(len(daily))) if len(daily) > 2 else float("nan")
            line += f"  {h} {sub[h].mean():+6.1f} (t {t:+.2f})"
        print(line)
