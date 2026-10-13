"""Diagnostic only: IDEA-028 v1 against an equal-weight book of every ranked eligible stock.

Same dataset, eligibility, calendar, rebalance sessions, next-open fills, end-of-data marking and
24 bps round-trip cost. The only difference: the universe book holds every stock that has a valid
252-return volatility on the rebalance day (no ranking, no top-30 cut). Writes nothing.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from signal_engine.backtest import panel, reproduce
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.backtest.strategies.low_volatility import LowVolatilityParams, return_volatility
from signal_engine.research.intake import freeze as intake_freeze

COST_BPS = 24.0
run = Path("signal_engine/research/ideas/idea-028-low-volatility/runs/2026-10-05-low-volatility-v1")
manifest = json.loads((run / "manifest.json").read_text())
frozen = intake_freeze.from_run_folder(run)
settings = {n: frozen.resolved[f]["value"] for n, f in rr._SETTINGS_FIELDS.items() if f in frozen.resolved}
settings.update(rr._intent_settings(frozen.intent))
res = reproduce._resolution_for(manifest)
with res.open() as source:
    col = DataCollector()
    frames = source.daily(res.eligible, None, res.contract.loader_min_sessions, col)
layer = rr._build_data_layer(frames, col, settings, as_of=None, adjusted_prices=True)
sessions = list(layer.calendar.trading_days)
idx = pd.DatetimeIndex([pd.Timestamp(d) for d in sessions])


def field_panel(name):
    cols = {}
    for s, f in layer.frames.items():
        ser = f[name].copy()
        ser.index = pd.DatetimeIndex(f.index.tz_localize(None).normalize() if f.index.tz else f.index.normalize())
        cols[s] = ser
    return pd.DataFrame(cols).reindex(idx)


open_, close = field_panel("Open"), field_panel("Close")
p = LowVolatilityParams()
vol = return_volatility(panel.close_panel(layer.frames, sessions), p.vol_lookback_returns)
rebalances = panel.monthly_rebalance_sessions(sessions)
pos = {d: i for i, d in enumerate(sessions)}
last = sessions[-1]


def book(holdings_by_rebalance):
    """Positions: buy at the open after the rebalance that adds a name, sell at the open after the
    rebalance that drops it; still held at the end is marked at the last close (EOD_DATA)."""
    trades, held = [], {}
    for d in rebalances:
        nxt = sessions[pos[d] + 1]
        new = set(holdings_by_rebalance[d])
        for s in sorted(set(held) - new):
            trades.append((s, held.pop(s), nxt, "exit_open"))
        for s in sorted(new - set(held)):
            held[s] = nxt
    for s, start in held.items():
        trades.append((s, start, last, "eod_close"))
    rows = []
    for s, a, b, how in trades:
        entry = open_.at[pd.Timestamp(a), s]
        exit_ = close.at[pd.Timestamp(b), s] if how == "eod_close" else open_.at[pd.Timestamp(b), s]
        g = (exit_ / entry - 1) * 1e4
        rows.append({"symbol": s, "entry_day": pd.Timestamp(a), "exit_day": pd.Timestamp(b),
                     "gross_bps": g, "net_bps": g - COST_BPS})
    return pd.DataFrame(rows)


low = panel.top_n_schedule(-vol, rebalances, p.max_positions, -np.inf)
universe = {d: list(vol.loc[pd.Timestamp(d)].dropna().index) for d in rebalances}
strat, univ = book(low), book(universe)

# Faithfulness: the panel reconstruction of the strategy book must equal the sealed trades.
sealed = pd.read_parquet(run / "evidence" / "trades.parquet")
sealed["entry_day"] = pd.to_datetime(sealed.entry_time).dt.tz_convert("Asia/Kolkata").dt.normalize().dt.tz_localize(None)
key = ["symbol", "entry_day"]
m = sealed.merge(strat, on=key, suffixes=("_sealed", "_panel"), how="outer", indicator=True)
assert (m["_merge"] == "both").all() and len(m) == 301, m["_merge"].value_counts()
gap = (m.net_bps_sealed - m.net_bps_panel).abs().max()
print(f"strategy book reconstructs the 301 sealed trades; max net bps difference {gap:.4f}")
assert gap < 0.5


def per_trade(df, label):
    print(f"{label:<10} trades {len(df):5d}  mean net {df.net_bps.mean():8.1f} bps  median {df.net_bps.median():7.1f}"
          f"  win {(df.net_bps > 0).mean():.3f}  mean hold {(df.exit_day - df.entry_day).dt.days.mean():5.0f} d")


print("\nPer-trade (the platform's convention; NOT comparable across books with different holding periods):")
per_trade(strat, "low-vol")
per_trade(univ, "universe")

# Time-weighted equal-weight books: each period runs from one execution open to the next; the
# final period ends at the last close. Turnover cost: 12 bps per side for each name added or dropped.
execs = [sessions[pos[d] + 1] for d in rebalances]
periods = []
prev_low, prev_univ = set(), set()
for i, d in enumerate(rebalances):
    a = pd.Timestamp(execs[i])
    end_close = i + 1 == len(rebalances)
    b = pd.Timestamp(last if end_close else execs[i + 1])
    exit_px = close.loc[b] if end_close else open_.loc[b]
    r = exit_px / open_.loc[a] - 1
    row = {"rebalance": d, "start": a, "end": b}
    for name, members, prev in (("low", set(low[d]), prev_low), ("univ", set(universe[d]), prev_univ)):
        if not members:
            row[name] = np.nan
            continue
        gross = float(r[list(members)].mean())
        changed = len(members - prev) + len(prev - members)
        cost = changed / max(len(members), 1) * 12.0 / 1e4
        row[name] = gross - cost
        row[name + "_n"] = len(members)
    prev_low, prev_univ = set(low[d]), set(universe[d])
    periods.append(row)
tw = pd.DataFrame(periods).dropna(subset=["low", "univ"])
tw["excess"] = tw.low - tw.univ
years = (tw.end.iloc[-1] - tw.start.iloc[0]).days / 365.25


def summary(series, label):
    growth = float((1 + series).prod())
    cagr = growth ** (1 / years) - 1
    vol_ = series.std(ddof=1) * np.sqrt(12)
    eq = (1 + series).cumprod()
    dd = float((eq / eq.cummax() - 1).min())
    print(f"{label:<10} CAGR {cagr:7.2%}  ann. vol {vol_:6.2%}  return/vol {cagr / vol_:5.2f}  max DD {dd:7.2%}  growth x{growth:5.2f}")


print(f"\nTime-weighted monthly books, {tw.start.iloc[0].date()} to {tw.end.iloc[-1].date()}, {len(tw)} periods, net of turnover cost:")
summary(tw.low, "low-vol")
summary(tw.univ, "universe")
print(f"universe book names per period: median {tw.univ_n.median():.0f} (min {tw.univ_n.min():.0f}, max {tw.univ_n.max():.0f})")
ex = tw.excess
t = ex.mean() / (ex.std(ddof=1) / np.sqrt(len(ex)))
print(f"monthly excess low-vol minus universe: mean {ex.mean() * 1e4:6.1f} bps  t {t:5.2f}  months positive {(ex > 0).mean():.1%}")
oos = tw.start >= pd.Timestamp(json.loads((run / "report" / "report.json").read_text())["split"]["oos_start"])
for label, part in (("before OOS start", ex[~oos]), ("from OOS start", ex[oos])):
    tt = part.mean() / (part.std(ddof=1) / np.sqrt(len(part)))
    print(f"  {label:<17} months {len(part):3d}  mean excess {part.mean() * 1e4:6.1f} bps  t {tt:5.2f}")
tw["year"] = tw.start.dt.year
yearly = tw.groupby("year").agg(low=("low", lambda s: (1 + s).prod() - 1), univ=("univ", lambda s: (1 + s).prod() - 1))
yearly["excess"] = yearly.low - yearly.univ
print("\nCalendar-year returns (by period start):")
print((yearly * 100).round(1).to_string())
down = tw.univ < 0
print(f"\nmonths the universe fell: {down.sum()}; low-vol mean {tw.low[down].mean() * 1e4:.0f} bps vs universe {tw.univ[down].mean() * 1e4:.0f} bps")
print(f"months the universe rose: {(~down).sum()}; low-vol mean {tw.low[~down].mean() * 1e4:.0f} bps vs universe {tw.univ[~down].mean() * 1e4:.0f} bps")
