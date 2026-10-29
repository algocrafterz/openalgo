"""Apply the pre-registered acceptance rule to results/summary_trials.csv (read-only)."""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
df = pd.read_csv(HERE / "results" / (sys.argv[1] if len(sys.argv) > 1 else "summary_trials.csv"))
base = df[df.trial == "B"].set_index("period").iloc[0:2] if False else df[df.trial == "B"].set_index("period")
cols = ["trial", "period", "n", "net4", "exp4", "exp8", "t4", "pf4", "dd4", "long_n", "long_exp4", "short_n", "short_exp4"]


def verdict(r, b):
    f = []
    if r.n < 30: f.append("n<30")
    if r.exp4 - b.exp4 < 0.03: f.append(f"dExp={r.exp4-b.exp4:+.3f}")
    if not r.pf4 > b.pf4: f.append("PF")
    if r.dd4 < b.dd4: f.append("DD")
    if r.long_exp4 < b.long_exp4 or r.short_exp4 < b.short_exp4: f.append("side")
    return f

print("| trial | period | n | net4 Rs | exp net4 R | exp net8 R | t | PF | maxDD Rs | long n/exp | short n/exp | fails |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|")
res = {}
for t in df.trial.unique():
    for per in ("P1", "P2"):
        r = df[(df.trial == t) & (df.period == per)].iloc[0]
        b = base.loc[per]
        f = [] if t == "B" else verdict(r, b)
        res.setdefault(t, {})[per] = f
        print(f"| {t} | {per} | {r.n} | {r.net4:.0f} | {r.exp4:+.3f} | {r.exp8:+.3f} | {r.t4:.2f} | {r.pf4:.2f} | {r.dd4:.0f} | {r.long_n}/{r.long_exp4:+.3f} | {r.short_n}/{r.short_exp4:+.3f} | {','.join(f) if t!='B' else '-'} |")
print()
for t, v in res.items():
    if t != "B" and not v["P1"] and not v["P2"]:
        print("PASSES BOTH:", t)
