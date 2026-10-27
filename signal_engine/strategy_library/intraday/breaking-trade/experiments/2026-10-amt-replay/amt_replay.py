"""AMT entry experiments for BREAKINGTRADE, rebuilt from scan_hits (isolated, read-only).

Usage, from the openalgo checkout:
    uv run python <this dir>/amt_replay.py <VARIANT> [FROM TO]
    VARIANT: B E1 E2 E3 E4 E5 E6 E7 E17          FROM/TO: ISO dates, TO exclusive
Default period 2026-09-24 onward. Writes results/amt_<VARIANT>_<FROM>.csv.

Rebuilds the live entry path (scan pick -> entry_watch confirmation -> order at the next scanner
poll) on Historify 1m bars, then applies ONE change. Exits, sizing and costs come from bt_replay
(the engine's staged TP ladder, no-progress gate, loss cut, 14:45 time exit). Reads
breakingtrade.db, trades.db and Historify read-only; writes only results/.

Live rules reproduced (analysis/breakingtrade):
  pick       scan_hits is_new=1; first_seen = first capture per (symbol, scan); direction per scan
  signal bar last 5m bar stamped <= first_seen (bars are stamped by their start)
  trigger    first COMPLETED 5m bar after it closing beyond its high (long) / low (short),
             seen at a poll no later than first_seen + 2h (entry_watch.CONFIRMATION_WINDOW)
  levels     IB 09:15-10:15 from bars before the poll; stop = far IB edge -/+ 0.25 x ATR
             (mean true range of the last 14 completed 5m bars); TP1 = entry +/- 1 x IB range
  gates      R:R to TP1 >= 0.75 and stop >= 0.20% of entry, on the signal (trigger close) price
  order      at the first poll at/after the trigger bar's close; fill = that minute's 1m open

Variants (each one change against B):
  E1   the trigger close must also be beyond the IB edge in the trade's direction, the IB as it
       stood before the trigger bar opened (unfinished before 10:15)
  E7   no trigger bar starting before 10:15 (the IB must be complete)
  E17  E1 and E7 together: the candidate
  E2   trigger = a completed 30-min period closing beyond the (complete) IB edge
  E3   stop = the broken IB edge -/+ 0.25 x ATR when the entry is beyond it
  E4   TP1 = nearest AMT reference beyond entry (1.5x/2x IB extension, PDH/PDL, prior VAH/VAL/POC)
  E5   Gap-Down Rescue only: stop under the rescue low, target the prior close
  E6   scan time windows (scans.SCANS) enforced on the pick time

Look-ahead check: every accepted trade's decision is recomputed with all bars from the poll
minute onward removed and must come out identical; a mismatch aborts the run.
"""

import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bt_replay as R  # noqa: E402

B = sqlite3.connect(f"file:{R.ROOT}/signal_engine/data/breakingtrade.db?mode=ro", uri=True)
WINDOW = dt.timedelta(hours=2)
IB_END = dt.time(10, 15)
LAST_ENTRY = dt.time(14, 45)
VARIANTS = ("B", "E1", "E2", "E3", "E4", "E5", "E6", "E7", "E17")
SCAN_WINDOWS = {  # scans.SCANS, documented windows
    "The Runaway": ("09:20", "10:30"),
    "The Breakdown": ("09:20", "10:30"),
    "Breakaway Above PDH": ("10:15", "13:00"),
    "Breakaway Below PDL": ("10:15", "13:00"),
    "Value Migration Up": ("10:30", "14:00"),
    "Value Migration Down": ("10:30", "14:00"),
    "The Gap-Up Trap": ("09:30", "11:30"),
    "Gap-Down Rescue": ("09:25", "11:00"),
    "Neutral Day Resolution Up": ("14:00", "15:15"),
    "Neutral Day Resolution Down": ("14:00", "15:15"),
}
VARIANT = sys.argv[1] if len(sys.argv) > 1 else "B"
FROM, TO = (sys.argv[2], sys.argv[3]) if len(sys.argv) > 3 else ("2026-09-24", "2099-01-01")


def polls(day):
    """Scanner poll minutes (__main__.POLL_WINDOWS) as datetimes."""
    out = []
    for start, end, marks in (
        (dt.time(9, 20), dt.time(10, 30), range(0, 60, 5)),
        (dt.time(10, 30), dt.time(13, 0), (1, 16, 31, 46)),
        (dt.time(14, 50), dt.time(15, 10), (50, 5, 10)),
    ):
        for h in range(start.hour, end.hour + 1):
            for m in marks:
                t = dt.time(h, m)
                if start <= t <= end:
                    out.append(dt.datetime.combine(day, t))
    return sorted(set(out))


def picks():
    rows = B.execute(
        "select symbol, scan, direction, min(captured_at) from scan_hits where is_new=1 "
        "and captured_at >= ? and captured_at < ? group by symbol, date(captured_at), scan order by 4",
        (FROM, TO),
    ).fetchall()
    return [
        {
            "sym": s,
            "scan": sc,
            "dir": "LONG" if d == "up" else "SHORT",
            "seen": dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
        }
        for s, sc, d, t in rows
    ]


def end_of(day, start, minutes):
    return (dt.datetime.combine(day, start) + dt.timedelta(minutes=minutes)).time()


def to5(df1):
    d = df1.copy()
    d["ts"] = [dt.datetime.combine(dt.date(2000, 1, 1), t) for t in d.t]
    g = d.set_index("ts").resample("5min", origin="start_day", offset="15min")
    out = g.agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    out["start"] = [x.time() for x in out.index]
    return out.reset_index(drop=True)


def to30(df1):
    d = df1.copy()
    d["p"] = [(t.hour * 60 + t.minute - 555) // 30 for t in d.t]
    g = d.groupby("p").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        n=("close", "size"),
    )
    g["end"] = [dt.time((555 + 30 * (p + 1)) // 60, (555 + 30 * (p + 1)) % 60) for p in g.index]
    return g[g.n >= 25].reset_index(drop=True)  # completed periods only


def atr(b5):
    if len(b5) < 2:
        return None
    tr = pd.concat(
        [b5.high - b5.low, (b5.high - b5.close.shift()).abs(), (b5.low - b5.close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    v = float(tr.tail(14).mean())
    return v if v > 0 else None


def prior_day(sym, day):
    """PDH, PDL, prior close and the prior session's 70% value area from its 1m volume profile."""
    row = R.H.execute(
        "select timestamp from market_data where symbol=? and exchange='NSE' and interval='D' "
        "and timestamp < ? order by timestamp desc limit 1",
        [sym, int(dt.datetime.combine(day, dt.time(0), R.IST).timestamp())],
    ).fetchone()
    if not row:
        return None
    pday = pd.to_datetime(row[0], unit="s", utc=True).tz_convert(R.IST).date()
    a = int(dt.datetime.combine(pday, dt.time(9, 15), R.IST).timestamp())
    b = int(dt.datetime.combine(pday, dt.time(15, 30), R.IST).timestamp())
    df = R.H.execute(
        "select high,low,close,volume from market_data where symbol=? and exchange='NSE' "
        "and interval='1m' and timestamp between ? and ? order by timestamp",
        [sym, a, b],
    ).df()
    if df.empty:
        return None
    step = max(0.05, df.close.iloc[-1] * 0.0005)
    px = ((df.high + df.low + df.close) / 3 / step).round() * step
    prof = df.assign(px=px).groupby("px").volume.sum().sort_index()
    idx = list(prof.index)
    lo = hi = idx.index(prof.idxmax())
    keep, total = prof.iloc[lo], prof.sum()
    while keep < 0.7 * total and (lo > 0 or hi < len(idx) - 1):
        up = prof.iloc[hi + 1] if hi < len(idx) - 1 else -1
        dn = prof.iloc[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1
            keep += up
        else:
            lo -= 1
            keep += dn
    return {
        "pdh": df.high.max(),
        "pdl": df.low.min(),
        "pclose": df.close.iloc[-1],
        "vah": idx[hi],
        "val": idx[lo],
        "poc": prof.idxmax(),
    }


def ib_as_of(df1, cutoff):
    """(IB high, IB low) from bars starting before min(cutoff, 10:15) - never a later bar."""
    w = df1[(df1.t < IB_END) & (df1.t < cutoff)]
    return (w.high.max(), w.low.min()) if not w.empty else (None, None)


def find_trigger(pk, df1, b5, sig_bar, poll_list):
    """(poll, trigger close, trigger bar start) under the variant's confirmation rule, or None."""
    s = 1 if pk["dir"] == "LONG" else -1
    seen, day = pk["seen"], pk["seen"].date()
    level = sig_bar.high if s > 0 else sig_bar.low
    cands = []
    if VARIANT == "E2":
        ibh, ibl = ib_as_of(df1, IB_END)
        for p in to30(df1).itertuples():
            if (
                p.end > seen.time()
                and p.end >= IB_END
                and s * (p.close - (ibh if s > 0 else ibl)) > 0
            ):
                cands.append((p.end, p.close, None))
    else:
        for bar in b5[b5.start > sig_bar.start].itertuples():
            end = end_of(day, bar.start, 5)
            if s * (bar.close - level) <= 0:
                continue
            if VARIANT in ("E7", "E17") and bar.start < IB_END:
                continue
            if VARIANT in ("E1", "E17"):
                # The IB as it stood before this bar opened (a bar cannot close beyond a high
                # that includes its own); before 10:15 that IB is still forming.
                ibh, ibl = ib_as_of(df1, bar.start)
                if ibh is None or s * (bar.close - (ibh if s > 0 else ibl)) <= 0:
                    continue
            cands.append((end, bar.close, bar.start))
    for end, close, start in cands:
        poll = next((p for p in poll_list if p.time() >= end), None)
        if poll is None or poll - seen > WINDOW or poll.time() >= LAST_ENTRY:
            return None
        return poll, close, start
    return None


def decide(pk, df1):
    """The trade decision from df1 alone: dict of levels, or a skip-reason string."""
    day = pk["seen"].date()
    if df1.empty:
        return "no_bars"
    b5 = to5(df1)
    sig = b5[b5.start <= pk["seen"].time()]
    if sig.empty:
        return "no_signal_bar"
    pl = [p for p in polls(day) if p >= pk["seen"]]
    trig = find_trigger(pk, df1, b5, sig.iloc[-1], pl)
    if trig is None:
        return "no_trigger"
    poll, sig_entry, trig_start = trig
    if trig_start is not None and end_of(day, trig_start, 5) > poll.time():
        return "trigger_after_poll"
    seen_1m = df1[df1.t < poll.time()]
    ibh, ibl = ib_as_of(seen_1m, poll.time())
    rng = (ibh - ibl) if ibh is not None else None
    done = [end_of(day, x, 5) <= poll.time() for x in b5.start]
    a = atr(b5[done])
    if not a or not rng or rng <= 0:
        return "no_levels"
    s = 1 if pk["dir"] == "LONG" else -1
    stop = ibl - 0.25 * a if s > 0 else ibh + 0.25 * a
    tp = sig_entry + s * rng
    changed = False
    if VARIANT == "E3":
        cand = ibh - 0.25 * a if s > 0 else ibl + 0.25 * a
        if s * (sig_entry - cand) >= R.MIN_SL_PCT * sig_entry:
            stop, changed = cand, True
    if VARIANT == "E4":
        pdx = prior_day(pk["sym"], day)
        refs = [ibh + 0.5 * rng, ibh + rng] if s > 0 else [ibl - 0.5 * rng, ibl - rng]
        if pdx:
            refs += (
                [pdx["pdh"], pdx["vah"], pdx["poc"], pdx["val"]]
                if s > 0
                else [pdx["pdl"], pdx["val"], pdx["poc"], pdx["vah"]]
            )
        beyond = [r for r in refs if s * (r - sig_entry) >= 0.25 * abs(sig_entry - stop)]
        if beyond:
            tp, changed = (min(beyond) if s > 0 else max(beyond)), True
    if VARIANT == "E5" and pk["scan"] == "Gap-Down Rescue":
        pdx = prior_day(pk["sym"], day)
        if not pdx or pdx["pclose"] <= sig_entry:
            return "e5_gap_already_filled"
        stop, tp, changed = seen_1m.low.min() - 0.25 * a, pdx["pclose"], True
    risk_sig = abs(sig_entry - stop)
    if (
        risk_sig <= 0
        or abs(tp - sig_entry) / risk_sig < R.MIN_RR
        or risk_sig / sig_entry < R.MIN_SL_PCT
    ):
        return "rr_or_minsl"
    return {
        "poll": poll,
        "sig_entry": sig_entry,
        "stop": stop,
        "tp": tp,
        "changed": changed,
        "trig_start": trig_start,
        "ibh": ibh,
        "ibl": ibl,
        "inside_ib": bool((sig_entry <= ibh) if s > 0 else (sig_entry >= ibl)),
    }


def build():
    rows, skipped, taken, cache = [], {}, set(), {}

    def skip(k):
        skipped[k] = skipped.get(k, 0) + 1

    for pk in picks():
        day = pk["seen"].date()
        key = (pk["sym"], day)
        if key in taken:
            continue
        if VARIANT == "E6":
            lo, hi = SCAN_WINDOWS.get(pk["scan"], ("00:00", "23:59"))
            if not (lo <= pk["seen"].strftime("%H:%M") <= hi):
                skip("outside_window")
                continue
        if key not in cache:
            cache[key] = R.bars(pk["sym"], day)
        df1 = cache[key]
        d = decide(pk, df1)
        if isinstance(d, str):
            skip(d)
            continue
        # Look-ahead check: the same decision from bars strictly before the poll minute.
        d2 = decide(pk, df1[df1.t < d["poll"].time()])
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
                "trig_start": d["trig_start"].strftime("%H:%M") if d["trig_start"] else "",
                "sig_entry": d["sig_entry"],
                "fill": fill,
                "stop": d["stop"],
                "tp": d["tp"],
                "changed": d["changed"],
                "inside_ib": d["inside_ib"],
                "ibh": d["ibh"],
                "ibl": d["ibl"],
                "df1": df1,
            }
        )
    return rows, skipped


def audit_e17(rows):
    """Every E17 trade: trigger bar starts at or after 10:15, closes beyond the complete IB edge,
    and closes no later than the poll that acts on it."""
    for r in rows:
        s = 1 if r["side"] == "LONG" else -1
        start = dt.time.fromisoformat(r["trig_start"])
        assert start >= IB_END, r
        assert s * (r["sig_entry"] - (r["ibh"] if s > 0 else r["ibl"])) > 0, r
        assert not r["inside_ib"], r
        assert r["poll"] >= end_of(dt.date(2000, 1, 1), start, 5).strftime("%H:%M"), r


def evaluate(rows):
    caps, cap_default = R.capital_by_day(R.signals())
    nd = R.nifty_dirs(sorted({r["day"] for r in rows}))
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
        # Filled at the poll minute's open; simulate() starts on the bar after it.
        move, why = R.simulate(r["df1"], t0, r["side"], r["fill"], r["stop"], r["tp"], 90)
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


if __name__ == "__main__":
    if VARIANT not in VARIANTS:
        sys.exit(f"variant must be one of {VARIANTS}")
    rows, skipped = build()
    if VARIANT == "E17":
        audit_e17(rows)
    d = evaluate(rows)
    d.to_csv(f"{R.OUT}/amt_{VARIANT}_{FROM}.csv", index=False)
    print(f"== {VARIANT} {FROM}..{TO} trades={len(d)} skipped={skipped} look-ahead check: passed")
    for col in ("gross", "net4", "net8"):
        print(f"  {col:5s}", R.metrics(d, col))
    for k in ("side", "nifty_at_entry", "nifty_day"):
        for g, sub in d.groupby(k):
            print(f"  {k}={g:5s}", R.metrics(sub))
    if len(d):
        print(
            "  exits",
            d.exit.value_counts().to_dict(),
            "changed",
            int(d.changed.sum()),
            "inside_ib",
            int(d.inside_ib.sum()),
        )
