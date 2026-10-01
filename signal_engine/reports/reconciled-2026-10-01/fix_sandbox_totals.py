"""Make the paper account's own totals (Positions page, Trading Dashboard) equal the strategy pages.

Run from the repo root. Dry run by default; --apply backs up db/sandbox.db first, then edits in one
transaction. The sandbox's trade and order history is NOT touched, so the stray orders stay visible
in the tradebook/orderbook as the audit trail; only the stored P&L totals are corrected.

Why: duplicate stop orders on 2026-10-01 bought a stray 855 ADANIPORTS (and a stray 32 TCS short) that no
strategy trade owns. The sandbox booked their P&L (-24,914.7 and +384.0) into its per-position and funds
totals, so Positions (-17,661.28) and the Dashboard (-17,936.48) disagreed with the P&L and Performance
pages (+6,861.77), which count strategy trades only.

What changes (analyze mode):
  sandbox_positions   each symbol's today_realized_pnl := what the strategy pages hold for it
                      (ADANIPORTS and TCS mainly; tiny sandbox rounding on others), and pnl /
                      accumulated_realized_pnl move by the same amount
  sandbox_funds       today_realized_pnl := the corrected position total; realized_pnl,
                      available_balance and total_pnl move by the same amount
"""

import sqlite3
import sys
from collections import defaultdict

DAY = "2026-10-01"
SANDBOX, CORE = "db/sandbox.db", "db/openalgo.db"
apply = "--apply" in sys.argv

core = sqlite3.connect(f"file:{CORE}?mode=ro", uri=True)
pages = defaultdict(float)
for sym, pnl in core.execute(
    "select symbol, sum(realized_pnl) from strategy_closed_trades where trade_date=? and mode='analyze' group by 1",
    (DAY,),
):
    pages[sym] += pnl
core.close()

con = sqlite3.connect(SANDBOX, timeout=30)
rows = con.execute(
    "select id, symbol, product, today_realized_pnl, pnl, accumulated_realized_pnl from sandbox_positions"
).fetchall()
plan = []
for rid, sym, prod, today, pnl, acc in rows:
    if sym not in pages:
        continue  # no strategy trades for this symbol today: leave the sandbox's own figure
    delta = round(pages[sym] - float(today or 0), 2)
    if abs(delta) >= 0.005:
        plan.append((rid, sym, float(today or 0), pages[sym], delta))
total_delta = round(sum(p[4] for p in plan), 2)
fid, f_today, f_real, f_avail, f_unreal = con.execute(
    "select id, today_realized_pnl, realized_pnl, available_balance, unrealized_pnl from sandbox_funds"
).fetchone()

pos_total_before = float(con.execute("select sum(today_realized_pnl) from sandbox_positions").fetchone()[0] or 0)
pos_total_after = round(pos_total_before + total_delta, 2)
funds_delta = round(pos_total_after - float(f_today or 0), 2)
print(f"strategy pages total (symbols they hold): {sum(pages.values()):,.2f}")
for rid, sym, old, new, delta in sorted(plan, key=lambda x: -abs(x[4])):
    print(f"  position {sym:12} today_realized {old:12,.2f} -> {new:12,.2f}  ({delta:+,.2f})")
print(f"Positions total: {pos_total_before:,.2f} -> {pos_total_after:,.2f}")
print(f"Dashboard realised (funds.today_realized_pnl): {float(f_today):,.2f} -> {pos_total_after:,.2f} ({funds_delta:+,.2f})")

if not apply:
    print("dry run; pass --apply")
    sys.exit(0)

bak = sqlite3.connect(SANDBOX + ".bak-reconcile-20261001")
con.backup(bak)
bak.close()
with con:
    for rid, sym, old, new, delta in plan:
        con.execute(
            "update sandbox_positions set today_realized_pnl=today_realized_pnl+?, pnl=pnl+?, "
            "accumulated_realized_pnl=accumulated_realized_pnl+? where id=?",
            (delta, delta, delta, rid),
        )
    con.execute(
        "update sandbox_funds set today_realized_pnl=today_realized_pnl+?, realized_pnl=realized_pnl+?, "
        "available_balance=available_balance+?, total_pnl=realized_pnl+? + unrealized_pnl where id=?",
        (funds_delta, funds_delta, funds_delta, funds_delta, fid),
    )
print("applied; backup at", SANDBOX + ".bak-reconcile-20261001")
