"""One-at-a-time parameter trials for BREAKINGTRADE, on top of the 2026-10-amt-replay tooling.

Read-only on every database. Reuses bt_replay (bars, sizing, costs, metrics) and amt_replay
(picks, polls, 5m bars, ATR, IB, trigger search) without editing them. Parameterised copies of
amt_replay.decide/build/evaluate and bt_replay.simulate live here; at default Params they must
reproduce baseline B exactly (see check_baseline).

Usage (from the openalgo checkout):
    uv run python <this dir>/tune.py check      # baseline equivalence vs recorded amt_B CSVs
    uv run python <this dir>/tune.py run        # every pre-registered trial, both periods
    uv run python <this dir>/tune.py run NAME [NAME ...]   # selected trials
"""

import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
AMT = HERE.parent / "2026-10-amt-replay"
sys.path.insert(0, str(AMT))
_argv = sys.argv
sys.argv = ["amt_replay.py", "B"]  # amt_replay reads argv at import
import amt_replay as A  # noqa: E402
import bt_replay as R  # noqa: E402

sys.argv = _argv
OUT = HERE / "results"
PERIODS = {"P1": ("2026-08-18", "2026-09-24"), "P2": ("2026-09-24", "2099-01-01")}
LADDER0 = ((1.0, 0.50), (1.5, 0.30), (2.0, 0.20))


@dataclasses.dataclass(frozen=True)
class Params:
    window_h: float = 2.0
    atr_buf: float = 0.25
    tp_mult: float = 1.0
    min_rr: float = 0.75
    np_prog: float = 0.20
    np_min: int = 90
    loss_prog: float | None = -0.80  # None = loss cut off
    ladder: tuple = LADDER0
    runner_buf: float = 0.3
    time_exit: dt.time = dt.time(14, 45)
    last_entry: dt.time = dt.time(14, 45)


def simulate(df, t0, side, entry, stop, tp, p):
    """bt_replay.simulate with every threshold taken from Params (same control flow)."""
    s = 1 if side == "LONG" else -1
    t0m = t0.hour * 60 + t0.minute
    tend = p.time_exit.hour * 60 + p.time_exit.minute
    tp_dist = abs(tp - entry)
    left_frac, booked, level_i = 1.0, 0.0, 0
    gate_done, pending = False, None
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
        while level_i < len(p.ladder):
            mult, frac = p.ladder[level_i]
            lvl = entry + s * mult * tp_dist
            if not ((s > 0 and r.high >= lvl) or (s < 0 and r.low <= lvl)):
                break
            take = min(frac, left_frac)
            booked += take * s * (lvl - entry)
            left_frac -= take
            level_i += 1
            if left_frac <= 1e-9:
                return booked, f"TP{mult:g}"
            stop = tighter(stop, lvl - s * p.runner_buf * mult * tp_dist)
        if gate_done:
            continue
        age = m + 1 - t0m
        prog = s * (r.close - entry) / tp_dist
        fired = (p.loss_prog is not None and age >= R.LOSS_CUT_AGE and prog < p.loss_prog) or (
            age >= p.np_min and prog < p.np_prog
        )
        if fired:
            gate_done = True
            rate = prog / age if age > 0 else 0.0
            remaining = tend - (m + 1)
            pending = "EXIT" if rate <= 0 or (1 - prog) / rate > remaining else "BE"
    last = df.iloc[-1]
    return close_rest(last.close, "EOD")


def decide(pk, df1, p):
    day = pk["seen"].date()
    if df1.empty:
        return "no_bars"
    b5 = A.to5(df1)
    sig = b5[b5.start <= pk["seen"].time()]
    if sig.empty:
        return "no_signal_bar"
    pl = [x for x in A.polls(day) if x >= pk["seen"]]
    trig = A.find_trigger(pk, df1, b5, sig.iloc[-1], pl)
    if trig is None:
        return "no_trigger"
    poll, sig_entry, trig_start = trig
    if trig_start is not None and A.end_of(day, trig_start, 5) > poll.time():
        return "trigger_after_poll"
    seen_1m = df1[df1.t < poll.time()]
    ibh, ibl = A.ib_as_of(seen_1m, poll.time())
    rng = (ibh - ibl) if ibh is not None else None
    done = [A.end_of(day, x, 5) <= poll.time() for x in b5.start]
    a = A.atr(b5[done])
    if not a or not rng or rng <= 0:
        return "no_levels"
    s = 1 if pk["dir"] == "LONG" else -1
    stop = ibl - p.atr_buf * a if s > 0 else ibh + p.atr_buf * a
    tp = sig_entry + s * p.tp_mult * rng
    risk_sig = abs(sig_entry - stop)
    if (
        risk_sig <= 0
        or abs(tp - sig_entry) / risk_sig < p.min_rr
        or risk_sig / sig_entry < R.MIN_SL_PCT
    ):
        return "rr_or_minsl"
    return {"poll": poll, "sig_entry": sig_entry, "stop": stop, "tp": tp, "trig_start": trig_start}


def build(p, frm, to, cache):
    A.FROM, A.TO = frm, to
    A.WINDOW = dt.timedelta(hours=p.window_h)
    A.LAST_ENTRY = p.last_entry
    rows, skipped, taken = [], {}, set()

    def skip(k):
        skipped[k] = skipped.get(k, 0) + 1

    for pk in A.picks():
        day = pk["seen"].date()
        key = (pk["sym"], day)
        if key in taken:
            continue
        if key not in cache:
            cache[key] = R.bars(pk["sym"], day)
        df1 = cache[key]
        d = decide(pk, df1, p)
        if isinstance(d, str):
            skip(d)
            continue
        d2 = decide(pk, df1[df1.t < d["poll"].time()], p)  # look-ahead check
        fields = ("poll", "sig_entry", "stop", "tp", "trig_start")
        if isinstance(d2, str) or any(d[f] != d2[f] for f in fields):
            raise AssertionError(f"look-ahead: {pk['sym']} {day} {d} vs {d2}")
        fill_row = df1[df1.t >= d["poll"].time()]
        if fill_row.empty:
            skip("no_fill_bar")
            continue
        fill = fill_row.open.iloc[0]
        s = 1 if pk["dir"] == "LONG" else -1
        if s * (fill - d["stop"]) <= 0 or s * (d["tp"] - fill) <= 0:
            skip("fill_beyond_stop_or_tp")
            continue
        taken.add(key)
        rows.append(
            {
                "sym": pk["sym"],
                "day": str(day),
                "scan": pk["scan"],
                "side": pk["dir"],
                "seen": pk["seen"].strftime("%H:%M"),
                "poll": d["poll"].strftime("%H:%M"),
                "sig_entry": d["sig_entry"],
                "fill": fill,
                "stop": d["stop"],
                "tp": d["tp"],
                "df1": df1,
            }
        )
    return rows, skipped


def evaluate(rows, p, caps, cap_default, nd):
    out = []
    for r in rows:
        s = 1 if r["side"] == "LONG" else -1
        t0 = dt.datetime.strptime(r["poll"], "%H:%M").time()
        cap = caps.get(r["day"], cap_default)
        qty = int(
            min(
                R.RISK * cap / (abs(r["sig_entry"] - r["stop"]) * (1 + R.SLIP_FACTOR)),
                cap / 0.20 / r["fill"],
            )
        )
        if qty <= 0:
            continue
        move, why = simulate(r["df1"], t0, r["side"], r["fill"], r["stop"], r["tp"], p)
        exit_px = r["fill"] + s * move
        gross = qty * move
        charges = float(
            R.SCHED.charge(
                buy_value=qty * (r["fill"] if s > 0 else exit_px),
                sell_value=qty * (exit_px if s > 0 else r["fill"]),
                orders=2,
            )
        )
        notional = qty * r["fill"]
        ndf, prev = nd[r["day"]]
        n_at = ndf[ndf.t < t0]
        out.append(
            dict(
                {k: v for k, v in r.items() if k != "df1"},
                qty=qty,
                risk_rs=qty * abs(r["fill"] - r["stop"]),
                exit=why,
                gross=gross,
                charges=charges,
                net4=gross - charges - 8e-4 * notional,
                net8=gross - charges - 16e-4 * notional,
                nifty_at_entry="up"
                if not n_at.empty and n_at.close.iloc[-1] >= ndf.open.iloc[0]
                else "down",
                nifty_day="up" if prev and not ndf.empty and ndf.close.iloc[-1] >= prev else "down",
            )
        )
    return pd.DataFrame(out)


def trial_grid():
    d = Params()
    r = dataclasses.replace
    t = {"B": d}
    t["win_1h"] = r(d, window_h=1.0)
    t["win_1.5h"] = r(d, window_h=1.5)
    t["atr_0"] = r(d, atr_buf=0.0)
    t["atr_0.5"] = r(d, atr_buf=0.5)
    t["tp_0.75"] = r(d, tp_mult=0.75)
    t["tp_1.25"] = r(d, tp_mult=1.25)
    t["rr_0.5"] = r(d, min_rr=0.5)
    t["rr_1.0"] = r(d, min_rr=1.0)
    t["np_prog_10"] = r(d, np_prog=0.10)
    t["np_prog_30"] = r(d, np_prog=0.30)
    t["np_min_120"] = r(d, np_min=120)
    t["loss_-0.6"] = r(d, loss_prog=-0.60)
    t["loss_off"] = r(d, loss_prog=None)
    t["ladder_tp1_100"] = r(d, ladder=((1.0, 1.0),))
    t["ladder_33_33_34"] = r(d, ladder=((1.0, 0.33), (1.5, 0.33), (2.0, 0.34)))
    t["runner_0.2"] = r(d, runner_buf=0.2)
    t["runner_0.5"] = r(d, runner_buf=0.5)
    t["texit_14:15"] = r(d, time_exit=dt.time(14, 15))
    t["texit_14:30"] = r(d, time_exit=dt.time(14, 30))
    t["last_entry_12:00"] = r(d, last_entry=dt.time(12, 0))
    t["last_entry_12:30"] = r(d, last_entry=dt.time(12, 30))
    # Exploratory neighbours of the two largest directional moves (counted as trials 22-23).
    t["tp_1.5"] = r(d, tp_mult=1.5)
    t["np_min_150"] = r(d, np_min=150)
    return t


def stats(d, col):
    m = R.metrics(d, col)
    return m


def summarise(name, period, d):
    g4, g8 = R.metrics(d, "net4"), R.metrics(d, "net8")
    row = {"trial": name, "period": period, "n": g4.get("n", 0)}
    if g4.get("n", 0) == 0:
        return row
    row.update(net4=g4["net"], exp4=g4["expR"], t4=g4["t"], pf4=g4["pf"], dd4=g4["maxdd"], exp8=g8["expR"])
    for side in ("LONG", "SHORT"):
        sub = d[d.side == side]
        m = R.metrics(sub, "net4")
        row[f"{side.lower()}_n"] = m.get("n", 0)
        row[f"{side.lower()}_exp4"] = m.get("expR")
    row["exits"] = ";".join(f"{k}:{v}" for k, v in d.exit.value_counts().items())
    return row


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    grid = trial_grid()
    names = sys.argv[2:] if len(sys.argv) > 2 else list(grid)
    if cmd == "check":
        names = ["B"]
    caps, cap_default = R.capital_by_day(R.signals())
    cache, nd_cache, rows_out = {}, {}, []
    for pname, (frm, to) in PERIODS.items():
        for name in names:
            p = grid[name]
            rows, skipped = build(p, frm, to, cache)
            days = sorted({x["day"] for x in rows} - set(nd_cache))
            nd_cache.update(R.nifty_dirs(days))
            d = evaluate(rows, p, caps, cap_default, nd_cache)
            d.to_csv(OUT / f"trial_{name.replace(':', '')}_{pname}.csv", index=False)
            s = summarise(name, pname, d)
            rows_out.append(s)
            print(name, pname, s, "skipped", skipped, flush=True)
        if cmd == "check":
            ref = pd.read_csv(AMT / "results" / f"amt_B_{frm}.csv")
            new = pd.read_csv(OUT / f"trial_B_{pname}.csv")
            cols = ["sym", "day", "side", "poll", "fill", "stop", "tp", "qty", "exit", "gross", "net4", "net8"]
            same = len(ref) == len(new) and ref[cols].round(6).equals(new[cols].round(6))
            print(f"BASELINE EQUIVALENCE {pname}: rows ref={len(ref)} new={len(new)} identical={same}")
    if cmd == "run":
        fname = "summary_trials.csv" if len(sys.argv) <= 2 else "summary_extra.csv"
        pd.DataFrame(rows_out).to_csv(OUT / fname, index=False)


if __name__ == "__main__":
    main()
