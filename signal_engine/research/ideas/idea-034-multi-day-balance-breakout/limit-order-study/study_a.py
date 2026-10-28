"""Sample A: IDEA-034 v1/v2 signals, market versus limit entries and limit targets (read-only).

    PYTHONPATH=. uv run python signal_engine/research/ideas/idea-034-multi-day-balance-breakout/limit-order-study/study_a.py

Rules are pre-registered in README.md. Inputs: the v1 run's evidence (signals, trades), the
balance edge from the v2 driver's height table (scratchpad trades_v2.pkl, cached to
results/edges.csv on first run) and Historify 1m bars. The published dataset is price-adjusted
and Historify is raw, so each trade's levels are scaled by recorded entry / raw 1m open at the
entry minute; bps are unaffected.
"""

import datetime as dt
import pickle
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from signal_engine.backtest.tick_reference import tick_for_price
from signal_engine.timeutils import IST

HERE = Path(__file__).resolve().parent
IDEA = HERE.parent
ROOT = HERE.parents[4]
EV = IDEA / "runs/2026-10-28-multi-day-balance-breakout-v1/evidence"
OUT = HERE / "results"
OUT.mkdir(exist_ok=True)
SCRATCH_V2 = Path(sys.argv[1]) if len(sys.argv) > 1 else None
CUT = dt.date(2023, 11, 8)
TIME_EXIT = dt.time(14, 45)
SLIP = 4.0
H = duckdb.connect(f"{ROOT}/db/historify.duckdb", read_only=True)


def edges(sig):
    cache = OUT / "edges.csv"
    if not cache.exists():
        hp = pickle.load(open(SCRATCH_V2, "rb"))["hp"]
        rows = [(s, str(d), a, b) for (s, d), (a, b) in hp.items()]
        pd.DataFrame(rows, columns=["symbol", "day", "h_hi", "h_lo"]).to_csv(cache, index=False)
    e = pd.read_csv(cache)
    sig = sig.merge(e, on=["symbol", "day"], how="left")
    long_ = sig.direction > 0
    sig["edge"] = np.where(
        long_, sig.target / (1 + sig.h_hi / 100), sig.target / (1 - sig.h_lo / 100)
    )
    sig["h_pct"] = np.where(long_, sig.h_hi, sig.h_lo)
    return sig


def load():
    tr = pd.read_parquet(EV / "trades.parquet")
    sg = pd.read_parquet(EV / "signals.parquet")
    d = tr.merge(sg[["signal_id", "direction", "signal_price", "stop", "target"]], on="signal_id")
    d["entry_ist"] = d.entry_time.dt.tz_convert(IST)
    d["day"] = d.entry_ist.dt.date.astype(str)
    d["stat_bps"] = d.cost_bps - d.cost_slippage_bps
    return edges(d)


def bars(sym, day):
    a = int(dt.datetime.combine(day, dt.time(9, 15), IST).timestamp())
    b = int(dt.datetime.combine(day, dt.time(15, 30), IST).timestamp())
    df = H.execute(
        "select timestamp,open,high,low,close from market_data where symbol=? and exchange='NSE' "
        "and interval='1m' and timestamp between ? and ? order by timestamp",
        [sym, a, b],
    ).df()
    df["t"] = pd.to_datetime(df.timestamp, unit="s", utc=True).dt.tz_convert(IST).dt.time
    return df


def run_exit(df, i0, s, entry, stop, target, tick, target_limit):
    """Walk 1m bars from row i0 (the fill bar, already open); return (exit price, reason, target
    filled as limit). Stop first when one bar reaches both."""
    for k in range(i0, len(df)):
        r = df.iloc[k]
        if r.t >= TIME_EXIT:
            return r.open, "TIME", False
        if k > i0 and ((s > 0 and r.open <= stop) or (s < 0 and r.open >= stop)):
            return r.open, "SL", False
        if (s > 0 and r.low <= stop) or (s < 0 and r.high >= stop):
            return stop, "SL", False
        if k == i0:
            continue  # the fill bar: only its stop is honoured (worst case)
        need = tick if target_limit else 0.0
        if (s > 0 and r.high >= target + need) or (s < 0 and r.low <= target - need):
            gap = (s > 0 and r.open > target) or (s < 0 and r.open < target)
            return (r.open if gap else target), "TP", target_limit
    return df.iloc[-1].close, "EOD", False


def simulate(t, df, variant, window, target_limit):
    """One signal -> dict(filled, gross, net) in bps of entry."""
    s = int(t.direction)
    f = t.factor
    stop, target, sigp, edge = t.stop / f, t.target / f, t.signal_price / f, t.edge / f
    day = dt.date.fromisoformat(t.day)
    i_mkt = df.index[df.t >= t.entry_ist.time()][0]
    if variant == "MKT":
        i_fill, entry, entry_slip = i_mkt, df.loc[i_mkt, "open"], SLIP
    else:
        lim = {"L0": sigp, "L1": edge, "L2": sigp - 0.25 * (sigp - edge)}[variant]
        tick = tick_for_price(lim, day)
        lim = round(round(lim / tick) * tick, 2)
        end = (dt.datetime.combine(day, t.entry_ist.time()) + dt.timedelta(minutes=window)).time()
        win = df.loc[i_mkt:]
        win = win[win.t < end]
        hit = win[(win.low <= lim - tick)] if s > 0 else win[(win.high >= lim + tick)]
        if hit.empty:
            return {"filled": False}
        i_fill = hit.index[0]
        op = df.loc[i_fill, "open"]
        entry = min(op, lim) if s > 0 else max(op, lim)  # a gap through the limit fills at open
        entry_slip = 0.0
        if s * (entry - stop) <= 0:
            g = s * (stop - entry) / entry * 1e4
            return {
                "filled": True,
                "gross": g,
                "reason": "SL",
                "net": g - t.stat_bps - SLIP,
                "net8": g - t.stat_bps - 2 * SLIP,
            }
    tick_t = tick_for_price(target, day)
    px, why, tgt_lim = run_exit(df, i_fill, s, entry, stop, target, tick_t, target_limit)
    gross = s * (px - entry) / entry * 1e4
    exit_slip = 0.0 if tgt_lim else SLIP
    return {
        "filled": True,
        "gross": gross,
        "reason": why,
        "net": gross - t.stat_bps - entry_slip - exit_slip,
        "net8": gross - t.stat_bps - 2 * entry_slip - 2 * exit_slip,
    }


VARIANTS = [("MKT", 0, False), ("MKT+T1", 0, True)] + [
    (f"{v}w{w}{'+T1' if tl else ''}", w, tl)
    for v in ("L0", "L1", "L2")
    for w in (15, 30)
    for tl in (False, True)
]


def main():
    d = load()
    rows = []
    for (sym, day), g in d.groupby(["symbol", "day"]):
        df = bars(sym, dt.date.fromisoformat(day))
        if df.empty:
            continue
        for t in g.itertuples():
            m = df[df.t >= t.entry_ist.time()]
            if m.empty:
                continue
            rec = {
                "signal_id": t.signal_id,
                "symbol": sym,
                "day": day,
                "dir": int(t.direction),
                "h_pct": t.h_pct,
                "rec_gross": t.gross_bps,
                "rec_net": t.net_bps,
                "rec_reason": t.exit_reason,
                "factor": t.entry_price / m.open.iloc[0],
            }
            t2 = pd.Series(t._asdict())
            t2["factor"] = rec["factor"]
            for name, w, tl in VARIANTS:
                base = name.split("w")[0].replace("+T1", "")
                r = simulate(t2, df, base, w, tl)
                rec[f"{name}|filled"] = r["filled"]
                rec[f"{name}|gross"] = r.get("gross", np.nan)
                rec[f"{name}|net"] = r.get("net", np.nan)
                rec[f"{name}|net8"] = r.get("net8", r["net"] - 8 if r["filled"] else np.nan)
                rec[f"{name}|reason"] = r.get("reason", "")
            rows.append(rec)
    out = pd.DataFrame(rows)
    out.to_csv(OUT / "sample_a.csv", index=False)
    print(f"signals replayed {len(out)} of {len(d)}; edge missing {int(d.edge.isna().sum())}")


if __name__ == "__main__":
    main()
