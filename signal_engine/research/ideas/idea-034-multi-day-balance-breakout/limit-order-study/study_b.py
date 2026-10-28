"""Sample B: BREAKINGTRADE baseline replay, market versus limit entries (read-only, shadow evidence).

    PYTHONPATH=. uv run python signal_engine/research/ideas/idea-034-multi-day-balance-breakout/limit-order-study/study_b.py

Signals come from the AMT replay's baseline (variant B) for P1 2026-08-18..09-23 and P2
2026-09-24..10-27; exits use that replay's own simulate() (staged targets, runner stop,
no-progress gate, 14:45). bps are of a 1,00,000 INR entry notional. Rules in README.md.
"""

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
REPLAY = HERE.parents[3] / "strategy_library/intraday/breaking-trade/experiments/2026-10-amt-replay"
sys.path.insert(0, str(REPLAY))
sys.argv = [sys.argv[0], "B"]
import amt_replay as A  # noqa: E402
import bt_replay as R  # noqa: E402

from signal_engine.backtest.tick_reference import tick_for_price  # noqa: E402

OUT = HERE / "results"
OUT.mkdir(exist_ok=True)
SLIP = 4.0
NOTIONAL = 1e5
PERIODS = (("2026-08-18", "2026-09-24"), ("2026-09-24", "2026-10-28"))


def stat_bps(s, entry, exit_px):
    q = NOTIONAL / entry
    buy, sell = (entry, exit_px) if s > 0 else (exit_px, entry)
    return float(R.SCHED.charge(buy_value=q * buy, sell_value=q * sell, orders=2)) / NOTIONAL * 1e4


def plus(t, minutes):
    return (dt.datetime.combine(dt.date(2000, 1, 1), t) + dt.timedelta(minutes=minutes)).time()


def one(r, variant, window):
    s = 1 if r["side"] == "LONG" else -1
    df, poll = r["df1"], dt.datetime.strptime(r["poll"], "%H:%M").time()
    day = dt.date.fromisoformat(r["day"])
    if variant == "MKT":
        entry, t0, slip_in = r["fill"], poll, SLIP
    else:
        sig = r["sig_entry"]
        edge = r["ibh"] if s > 0 else r["ibl"]
        if s * (sig - edge) <= 0:
            edge = sig  # entry not beyond the IB edge: L1 = L2 = L0
        lim = {"L0": sig, "L1": edge, "L2": sig - 0.25 * (sig - edge)}[variant]
        tick = tick_for_price(lim, day)
        lim = round(round(lim / tick) * tick, 2)
        win = df[(df.t >= poll) & (df.t < plus(poll, window))]
        hit = win[win.low <= lim - tick] if s > 0 else win[win.high >= lim + tick]
        if hit.empty:
            return {"filled": False}
        bar = hit.iloc[0]
        entry = min(bar.open, lim) if s > 0 else max(bar.open, lim)
        t0, slip_in = bar.t, 0.0
        if (s > 0 and bar.low <= r["stop"]) or (s < 0 and bar.high >= r["stop"]):
            g = s * (r["stop"] - entry) / entry * 1e4  # stopped on the fill bar (worst case)
            st = stat_bps(s, entry, r["stop"])
            return {
                "filled": True,
                "gross": g,
                "net": g - st - SLIP,
                "net8": g - st - 2 * SLIP,
                "reason": "SL",
            }
    if s * (r["tp"] - entry) <= 0 or s * (entry - r["stop"]) <= 0:
        return {"filled": False}
    move, why = R.simulate(df, t0, r["side"], entry, r["stop"], r["tp"], 90)
    g = move / entry * 1e4
    st = stat_bps(s, entry, entry + s * move)
    return {
        "filled": True,
        "gross": g,
        "net": g - st - slip_in - SLIP,
        "net8": g - st - 2 * slip_in - 2 * SLIP,
        "reason": why,
        "move": move,
    }


def main():
    calib = pd.concat(pd.read_csv(REPLAY / f"results/amt_B_{f}.csv") for f, _ in PERIODS).set_index(
        ["sym", "day"]
    )
    rows = []
    for f, t in PERIODS:
        A.FROM, A.TO = f, t
        built, _ = A.build()
        for r in built:
            rec = {"sym": r["sym"], "day": r["day"], "dir": 1 if r["side"] == "LONG" else -1}
            for v in ("MKT", "L0", "L1", "L2"):
                for w in (0,) if v == "MKT" else (15, 30):
                    name = v if v == "MKT" else f"{v}w{w}"
                    o = one(r, v, w)
                    rec[f"{name}|filled"] = o["filled"]
                    for k in ("gross", "net", "net8", "reason"):
                        rec[f"{name}|{k}"] = o.get(k)
                    if v == "MKT":
                        key = (r["sym"], r["day"])
                        if key in calib.index:  # absent when the replay sized it to 0 shares
                            c = calib.loc[[key]].iloc[0]
                            rec["calib_diff"] = o["move"] - c.gross / c.qty
            rows.append(rec)
    pd.DataFrame(rows).to_csv(OUT / "sample_b.csv", index=False)
    print(f"signals {len(rows)}")


if __name__ == "__main__":
    main()
