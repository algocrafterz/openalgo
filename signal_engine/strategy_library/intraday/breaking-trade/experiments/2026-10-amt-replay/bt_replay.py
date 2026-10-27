"""Isolated exit/stop replay for BREAKINGTRADE paper signals on Historify 1m bars.

Read-only: data/trades.db, data/breakingtrade.db, db/historify.duckdb. Writes only CSVs in the
scratchpad. Mirrors the engine as configured (config.yaml, tracker.py):
  * entry at the paper order time (executed_at) and price (fill_price, else signal entry)
  * broker SL-M at the planned stop; fills at the stop, or at the bar open when it gaps through
  * single TP1 (100%), filled at the TP level when a bar trades through it
  * loss-cut gate (age >= 20 min, progress < -0.80) then main no-progress gate (age >= N min,
    progress < 0.20), each checked at a 1m bar CLOSE and acted on at the NEXT bar open:
    market exit if the pace cannot reach TP by 14:45, else stop to the fill price; once only
  * time exit 14:45 at that bar's open
No bar after the decision minute is used for any decision. An SL and TP inside the same bar
count as SL (conservative).
"""

import collections
import datetime as dt
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = str(HERE.parents[5])  # the openalgo checkout
OUT = str(HERE / "results")
sys.path.insert(0, ROOT)
from portfolio.costs import india_intraday  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TIME_EXIT = dt.time(14, 45)
MIN_RR, MIN_SL_PCT, SLIP_FACTOR, RISK = 0.75, 0.002, 0.10, 0.01
LOSS_CUT_AGE, LOSS_CUT_PROG, NP_PROG = 20, -0.80, 0.20
SCHED = india_intraday("NSE", broker="flattrade")

H = duckdb.connect(f"{ROOT}/db/historify.duckdb", read_only=True)
T = sqlite3.connect(f"file:{ROOT}/signal_engine/data/trades.db?mode=ro", uri=True)
B = sqlite3.connect(f"file:{ROOT}/signal_engine/data/breakingtrade.db?mode=ro", uri=True)


def bars(sym, day):
    a = int(dt.datetime.combine(day, dt.time(9, 15), IST).timestamp())
    b = int(dt.datetime.combine(day, dt.time(15, 30), IST).timestamp())
    df = H.execute(
        "select timestamp,open,high,low,close from market_data where symbol=? and exchange=? "
        "and interval='1m' and timestamp between ? and ? order by timestamp",
        [sym, "NSE_INDEX" if sym == "NIFTY" else "NSE", a, b],
    ).df()
    df["t"] = pd.to_datetime(df.timestamp, unit="s", utc=True).dt.tz_convert(IST).dt.time
    return df


def signals():
    """One signal per (symbol, day): the executed one, else the first R:R-declined one."""
    rows = T.execute(
        "select symbol,direction,entry,sl,tp,quantity,executed_at,fill_price,status,message "
        "from trades where strategy='BREAKINGTRADE' and trade_mode='analyze' "
        "and direction in ('LONG','SHORT') and (status='SUCCESS' or "
        "(status='DECLINED' and message like '%R:R%')) order by executed_at"
    ).fetchall()
    executed, declined = [], {}
    for sym, side, e, sl, tp, q, ts, fill, status, _ in rows:
        sig = {
            "sym": sym,
            "side": side,
            "entry": e,
            "sl": sl,
            "tp": tp,
            "qty": q,
            "ts": ts,
            "fill": fill or e,
            "status": status,
        }
        if status == "SUCCESS":
            executed.append(sig)
        else:
            declined.setdefault((sym, ts[:10]), sig)  # first refusal per symbol and day
    traded = {(x["sym"], x["ts"][:10]) for x in executed}
    return executed + [v for k, v in declined.items() if k not in traded]


def actual_exits():
    act = collections.defaultdict(lambda: [0.0, []])
    for sym, d, ctx in T.execute(
        "select symbol,date(executed_at),context from trades where strategy='BREAKINGTRADE' "
        "and trade_mode='analyze' and status='SUCCESS' and direction='EXIT'"
    ):
        c = json.loads(ctx or "{}")
        if c.get("pnl") is not None:
            act[(sym, d)][0] += c["pnl"]
            act[(sym, d)][1] += c.get("exit_types") or []
    return act


def tail_flag(sym, ts):
    r = B.execute(
        "select tail from snapshots where kind='market_profile' and symbol=? and captured_at<=? "
        "and captured_at>=? order by captured_at desc limit 1",
        (sym, ts[:19].replace("T", " "), ts[:10] + " 09:00:00"),
    ).fetchone()
    return r[0] if r else None


def tail_levels(df_before, side):
    """(outer, inner) of the tail at the day's extreme from completed 30-min periods, or None."""
    if df_before.empty:
        return None
    mins = [(t.hour * 60 + t.minute - 555) // 30 for t in df_before.t]
    g = df_before.assign(p=mins).groupby("p").agg(high=("high", "max"), low=("low", "min"))
    if len(g) < 2:
        return None
    last = g.index.max()
    if side == "LONG":
        p_ext = g.low.idxmin()
        if p_ext == last or (g.low == g.low.min()).sum() > 1:
            return None
        outer, inner = g.low.min(), g.drop(p_ext).low.min()
        return (outer, inner) if inner > outer else None
    p_ext = g.high.idxmax()
    if p_ext == last or (g.high == g.high.max()).sum() > 1:
        return None
    outer, inner = g.high.max(), g.drop(p_ext).high.max()
    return (outer, inner) if inner < outer else None


#: tp_watch.DEFAULT_SPLIT as fractions of the ORIGINAL position at 1.0 / 1.5 / 2.0 x the TP1
#: distance; after each level the runner stop goes to that level minus 0.3 x its own R
#: (main._replace_runner_sl with use_extended_runner_tiers, no structural level), tighten-only.
LADDER = ((1.0, 0.50), (1.5, 0.30), (2.0, 0.20))
RUNNER_BUFFER = 0.3


def simulate(df, t0, side, entry, stop, tp, np_minutes):
    """Per-share P&L of the whole position (fraction-weighted legs) and the closing reason."""
    s = 1 if side == "LONG" else -1
    t0m = t0.hour * 60 + t0.minute
    tend = TIME_EXIT.hour * 60 + TIME_EXIT.minute
    tp_dist = abs(tp - entry)
    left_frac, booked, level_i = 1.0, 0.0, 0
    gate_done, pending = False, None  # pending: action decided at a close, done at next open
    tighter = max if s > 0 else min

    def close_rest(px, why):
        return booked + left_frac * s * (px - entry), why

    for r in df[df.t > t0].itertuples():
        m = r.t.hour * 60 + r.t.minute
        if m >= tend:
            return close_rest(r.open, "TIME")
        if pending == "EXIT":
            return close_rest(r.open, "NO-PROGRESS")
        if pending == "BE":
            stop, pending = tighter(stop, entry), None
        if (s > 0 and r.open <= stop) or (s < 0 and r.open >= stop):
            return close_rest(r.open, "SL" if level_i == 0 and not gate_done else "STOP-AFTER")
        if (s > 0 and r.low <= stop) or (s < 0 and r.high >= stop):
            return close_rest(stop, "SL" if level_i == 0 and not gate_done else "STOP-AFTER")
        while level_i < len(LADDER):
            mult, frac = LADDER[level_i]
            lvl = entry + s * mult * tp_dist
            if not ((s > 0 and r.high >= lvl) or (s < 0 and r.low <= lvl)):
                break
            take = min(frac, left_frac)
            booked += take * s * (lvl - entry)
            left_frac -= take
            level_i += 1
            if left_frac <= 1e-9:
                return booked, f"TP{mult:g}"
            stop = tighter(stop, lvl - s * RUNNER_BUFFER * mult * tp_dist)
        if gate_done:
            continue
        age = m + 1 - t0m  # minutes elapsed at this bar's close
        prog = s * (r.close - entry) / tp_dist
        fired = (age >= LOSS_CUT_AGE and prog < LOSS_CUT_PROG) or (
            age >= np_minutes and prog < NP_PROG
        )
        if fired:
            gate_done = True
            rate = prog / age if age > 0 else 0.0
            remaining = tend - (m + 1)
            pending = "EXIT" if rate <= 0 or (1 - prog) / rate > remaining else "BE"
    last = df.iloc[-1]
    return close_rest(last.close, "EOD")


def capital_by_day(sigs):
    caps = collections.defaultdict(list)
    for x in sigs:
        if x["status"] == "SUCCESS" and x["qty"]:
            caps[x["ts"][:10]].append(
                x["qty"] * abs(x["entry"] - x["sl"]) * (1 + SLIP_FACTOR) / RISK
            )
    med = st.median([c for v in caps.values() for c in v])
    return {d: st.median(v) for d, v in caps.items()}, med


def nifty_dirs(days):
    out = {}
    for d in days:
        df = bars("NIFTY", dt.date.fromisoformat(d))
        prev = H.execute(
            "select close from market_data where symbol='NIFTY' and exchange='NSE_INDEX' "
            "and interval='D' and timestamp < ? order by timestamp desc limit 1",
            [int(dt.datetime.combine(dt.date.fromisoformat(d), dt.time(0), IST).timestamp())],
        ).fetchone()
        out[d] = (df, prev[0] if prev else None)
    return out


def run(variant, np_minutes=90, stop_mode="ib"):
    sigs = signals()
    caps, cap_default = capital_by_day(sigs)
    act = actual_exits()
    nd = nifty_dirs(sorted({x["ts"][:10] for x in sigs}))
    rows, skipped = [], collections.Counter()
    for x in sigs:
        day = dt.date.fromisoformat(x["ts"][:10])
        t0 = dt.time.fromisoformat(x["ts"][11:19])
        df = bars(x["sym"], day)
        if df.empty:
            skipped["no_bars"] += 1
            continue
        before = df[df.t < dt.time(t0.hour, t0.minute)]
        side, e = x["side"], x["fill"]
        ib = before[before.t < dt.time(10, 15)]
        stop = x["sl"]
        tail_used = False
        if stop_mode != "ib":
            if ib.empty:
                skipped["no_ib"] += 1
                continue
            atr = ((ib.low.min() - x["sl"]) if side == "LONG" else (x["sl"] - ib.high.max())) / 0.25
            flag = tail_flag(x["sym"], x["ts"])
            want = "buy_tail" if side == "LONG" else "sell_tail"
            lv = tail_levels(before, side) if flag == want and atr > 0 else None
            if lv is not None:
                level = lv[0] if stop_mode == "tail_outer" else lv[1]
                cand = level - 0.25 * atr if side == "LONG" else level + 0.25 * atr
                if (side == "LONG" and cand < e) or (side == "SHORT" and cand > e):
                    stop, tail_used = cand, True
        risk_ps = abs(e - stop)
        # The validator gates on the SIGNAL price; an executed signal whose stop is unchanged
        # already passed, so it is never re-gated.
        sig_risk = abs(x["entry"] - stop)
        rr = abs(x["tp"] - x["entry"]) / sig_risk if sig_risk else 0
        regate = x["status"] != "SUCCESS" or stop != x["sl"]
        if regate and (rr < MIN_RR or sig_risk / x["entry"] < MIN_SL_PCT):
            skipped["gate"] += 1
            continue
        if risk_ps <= 0:
            skipped["stop_through_fill"] += 1
            continue
        cap = caps.get(x["ts"][:10], cap_default)
        qty = int(min(RISK * cap / (risk_ps * (1 + SLIP_FACTOR)), cap / 0.20 / e))
        if qty <= 0:
            skipped["qty0"] += 1
            continue
        move, why = simulate(df, t0, side, e, stop, x["tp"], np_minutes)
        exit_px = e + (move if side == "LONG" else -move)
        gross = qty * move
        charges = float(
            SCHED.charge(
                buy_value=qty * (e if side == "LONG" else exit_px),
                sell_value=qty * (exit_px if side == "LONG" else e),
                orders=2,
            )
        )
        notional = qty * e
        nifty_df, prev = nd[x["ts"][:10]]
        n_at = nifty_df[nifty_df.t < dt.time(t0.hour, t0.minute)]
        rows.append(
            {
                "sym": x["sym"],
                "day": x["ts"][:10],
                "side": side,
                "status": x["status"],
                "tail_used": tail_used,
                "exit": why,
                "qty": qty,
                "aqty": x["qty"],
                "move": move,
                "risk_rs": qty * risk_ps,
                "gross": gross,
                "charges": charges,
                "net4": gross - charges - 2 * 4e-4 * notional,
                "net8": gross - charges - 2 * 8e-4 * notional,
                "nifty_at_entry": "up"
                if not n_at.empty and n_at.close.iloc[-1] >= nifty_df.open.iloc[0]
                else "down",
                "nifty_day": (
                    "up"
                    if prev and not nifty_df.empty and nifty_df.close.iloc[-1] >= prev
                    else "down"
                ),
                "actual": act.get((x["sym"], x["ts"][:10]), [None])[0],
                "actual_exit": ",".join(act.get((x["sym"], x["ts"][:10]), [0, []])[1]),
            }
        )
    d = pd.DataFrame(rows)
    d.to_csv(f"{OUT}/replay_{variant}.csv", index=False)
    return d, skipped


def metrics(d, col="net4"):
    if d.empty:
        return {"n": 0}
    r = d[col] / d.risk_rs
    daily = d.groupby("day")[col].sum().sort_index().cumsum()
    dd = (daily - daily.cummax()).min()
    wins, losses = d[col][d[col] > 0].sum(), -d[col][d[col] <= 0].sum()
    return {
        "n": len(d),
        "net": round(d[col].sum()),
        "expR": round(r.mean(), 3),
        "t": round(r.mean() / (r.std() / len(r) ** 0.5), 2) if len(r) > 1 else None,
        "pf": round(wins / losses, 2) if losses else None,
        "maxdd": round(dd),
    }


if __name__ == "__main__":
    variant = sys.argv[1]
    np_min = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    mode = sys.argv[3] if len(sys.argv) > 3 else "ib"
    executed_only = mode == "ib"
    d, skipped = run(variant, np_min, mode)
    if executed_only:
        d = d[d.status == "SUCCESS"]
    print(f"== {variant} np={np_min} stop={mode} skipped={dict(skipped)}")
    for col in ("gross", "net4", "net8"):
        print(f"  {col:5s}", metrics(d, col))
    for k in ("side", "nifty_at_entry", "nifty_day"):
        for g, sub in d.groupby(k):
            print(f"  {k}={g:5s}", metrics(sub))
    print(
        "  exits",
        d.exit.value_counts().to_dict(),
        "tail_used",
        int(d.tail_used.sum()),
        "added_from_declined",
        int((d.status != "SUCCESS").sum()),
    )
