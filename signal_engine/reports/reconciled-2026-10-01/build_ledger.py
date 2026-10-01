"""Rebuild 2026-10-01 BREAKOUT and BREAKOUT-PY trades from the sandbox's own fills.

Run from the repo root (re-run after 15:30 IST so open positions are final):
    python3 signal_engine/reports/reconciled-2026-10-01/build_ledger.py

Source of truth is db/sandbox.db (sandbox_trades fills joined to sandbox_orders by order id, which
carry the strategy tag). signal_engine/data/trades.db is only used as a cross-check. Both are opened
read-only. Known stray orders are excluded by id, each with the evidence for why.
"""

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

DAY = "2026-10-01"
OUT = Path(__file__).parent
sb = sqlite3.connect("file:db/sandbox.db?mode=ro", uri=True)
tdb = sqlite3.connect("file:signal_engine/data/trades.db?mode=ro", uri=True)

ARTIFACT = {
    "26100188576528": "Pine TP1 alert for a trade its entry guard had already closed; it sold 32 BREAKOUT-PY TCS shares",
    **{
        o: "duplicate ADANIPORTS stop re-sent after client timeouts"
        for o in (
            "26100137242362",
            "26100113548158",
            "26100164469338",
            "26100167574299",
            "26100105487029",
        )
    },
}
# Trips whose pairing the strategy rules settle, not the fills alone: key -> (pnl, exit_px, note).
MANUAL = {
    ("BREAKOUT-PY", "ADANIPORTS", "11:21:09"): (
        -1008.9,
        1768.4,
        "short 171 @ 1762.5 stopped out at 1768.4 (the Ladder logged its stop at 1768.29 at 11:45 and the "
        "sandbox filled the stop at 1768.4); the 11:55 market BUY at 1761.6 (+153.9) is a safety-net artifact "
        "of the duplicate-stop bug. This is also how the strategy book pairs the fills.",
    ),
}
GUARD_ABORT = {
    ("BREAKOUT", "TCS", "10:15:12"): "entry guard closed it: fill 2091.0 slipped 0.89R from signal 2085.0 (max 0.50R)",
    ("BREAKOUT-PY", "COFORGE", "10:25:35"): "entry guard closed it: fill 1807.0 slipped 0.62R from signal 1802.0 (max 0.50R)",
}

fills = defaultdict(list)
for oid, q, p in sb.execute(
    "select orderid, quantity, price from sandbox_trades where date(trade_timestamp)=?", (DAY,)
):
    fills[oid].append((q, p))
orders = sb.execute(
    "select orderid,strategy,symbol,action,order_timestamp from sandbox_orders "
    "where date(order_timestamp)=? and strategy in ('BREAKOUT','BREAKOUT-PY') "
    "and order_status='complete' order by order_timestamp",
    (DAY,),
).fetchall()

# engine view: per (strategy, symbol) trip -> final cumulative pnl
eng = defaultdict(list)
open_q = {}
for strat, sym, d, q, pnl in tdb.execute(
    "select strategy,symbol,direction,quantity,json_extract(context,'$.pnl') from trades "
    "where date(executed_at)=? and strategy in ('BREAKOUT','BREAKOUT-PY') "
    "and order_id not in ('','-DECLINED-') order by id",
    (DAY,),
):
    k = (strat, sym)
    if d in ("LONG", "SHORT"):
        open_q[k] = [q, 0, None]
    elif k in open_q:
        open_q[k][1] += q
        open_q[k][2] = pnl
        if open_q[k][1] >= open_q[k][0]:
            eng[k].append(open_q.pop(k)[2])

legs = defaultdict(list)
for oid, strat, sym, act, ts in orders:
    f = fills.get(oid)
    if f:
        qty = sum(x[0] for x in f)
        legs[(strat, sym)].append(
            dict(oid=oid, act=act, qty=qty, px=sum(a * b for a, b in f) / qty, ts=ts[11:19])
        )

rows = []
for (strat, sym), ls in sorted(legs.items()):
    pos, cost, cur, ntrip = 0, 0.0, None, 0
    for L in ls:
        if L["oid"] in ARTIFACT:
            rows.append(dict(strategy=strat, symbol=sym, status="EXCLUDED_ARTIFACT", side="", entry_time=L["ts"],
                             entry_px="", exit_time="", exit_px="", qty=L["qty"], ledger_pnl="", engine_pnl="",
                             note=f"{L['act']} {L['qty']} @ {L['px']:.2f}: {ARTIFACT[L['oid']]}"))
            continue
        s = 1 if L["act"] == "BUY" else -1
        if pos == 0:
            pos, cost = s * L["qty"], L["px"]
            cur = dict(t=L["ts"], px=L["px"], side=s, q=L["qty"], pnl=0.0)
        elif (pos > 0) == (s > 0):
            cost = (cost * abs(pos) + L["px"] * L["qty"]) / (abs(pos) + L["qty"])
            pos += s * L["qty"]
        else:
            c = min(L["qty"], abs(pos))
            cur["pnl"] += (L["px"] - cost) * c * (1 if pos > 0 else -1)
            pos += s * c
            if pos == 0:
                e = eng.get((strat, sym), [])
                ep = e[ntrip] if ntrip < len(e) else None
                ntrip += 1
                key = (strat, sym, cur["t"])
                if key in GUARD_ABORT:
                    status, note = "GUARD_ABORT", GUARD_ABORT[key]
                elif key in MANUAL:
                    status, note = "CORRECTED", MANUAL[key][2]
                    cur["pnl"], L["px"] = MANUAL[key][0], MANUAL[key][1]
                elif ep is None:
                    status, note = "CORRECTED", "engine recorded no close for this trade; ledger uses sandbox fills"
                elif abs(ep - cur["pnl"]) <= 1.0:
                    status, note = "VERIFIED", ""
                else:
                    status, note = "CORRECTED", f"engine booked {ep:.1f} (ladder price, not the real fill); ledger uses sandbox fills"
                rows.append(dict(strategy=strat, symbol=sym, status=status, side="LONG" if cur["side"] > 0 else "SHORT",
                                 entry_time=cur["t"], entry_px=round(cur["px"], 2), exit_time=L["ts"],
                                 exit_px=round(L["px"], 2), qty=cur["q"], ledger_pnl=round(cur["pnl"], 1),
                                 engine_pnl="" if ep is None else round(ep, 1), note=note))
    if pos != 0:
        rows.append(dict(strategy=strat, symbol=sym, status="OPEN", side="LONG" if cur["side"] > 0 else "SHORT",
                         entry_time=cur["t"], entry_px=round(cur["px"], 2), exit_time="", exit_px="", qty=abs(pos),
                         ledger_pnl=round(cur["pnl"], 1), engine_pnl="",
                         note="ledger_pnl is realised on closed legs only; remaining quantity still open"))

fields = ["strategy", "symbol", "status", "side", "entry_time", "entry_px", "exit_time", "exit_px", "qty", "ledger_pnl", "engine_pnl", "note"]
with open(OUT / "ledger.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)

usable = ("VERIFIED", "CORRECTED", "GUARD_ABORT")
for strat in ("BREAKOUT", "BREAKOUT-PY"):
    t = [r for r in rows if r["strategy"] == strat and r["status"] in usable]
    pnl = sum(r["ledger_pnl"] for r in t)
    win = sum(1 for r in t if r["ledger_pnl"] > 0)
    print(f"{strat}: usable trades={len(t)} wins={win} pnl={pnl:.1f}")
from collections import Counter
print(Counter((r["strategy"], r["status"]) for r in rows))
