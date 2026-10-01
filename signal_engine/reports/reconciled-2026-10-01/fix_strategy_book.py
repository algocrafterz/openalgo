"""Remove the phantom open legs stray orders left in the strategy book (Strategy P&L / Performance pages).

Dry run by default; --apply backs up db/openalgo.db first, then zeroes the legs in one transaction.
Safe to re-run (idempotent). Run from the repo root.

The pages read strategy_positions / strategy_closed_trades, which are already booked from real fills,
so closed-trade P&L needed no change (it matches ledger.csv to the rupee). What was wrong: two open
legs that only exist because of stray orders, which show as open positions with unrealised P&L:
  BREAKOUT     TCS         -32  from Pine's TP1 alert sold against BREAKOUT-PY's shares
  BREAKOUT-PY  ADANIPORTS  855  from five duplicate stop orders sent after server timeouts
"""

import sqlite3
import sys

DB = "db/openalgo.db"
LEGS = [("BREAKOUT", "TCS"), ("BREAKOUT-PY", "ADANIPORTS")]
apply = "--apply" in sys.argv

con = sqlite3.connect(DB, timeout=30)
rows = []
for strat, sym in LEGS:
    rows += con.execute(
        "select id,strategy,symbol,quantity,average_price,realized_pnl from strategy_positions "
        "where mode='analyze' and strategy=? and symbol=? and quantity!=0",
        (strat, sym),
    ).fetchall()
for r in rows:
    print("phantom open leg:", r)
if not rows:
    print("nothing to fix")
elif apply:
    bak = sqlite3.connect(DB + ".bak-reconcile-20261001")
    con.backup(bak)
    bak.close()
    with con:
        con.executemany("update strategy_positions set quantity=0, average_price=0 where id=?", [(r[0],) for r in rows])
    print("applied; backup at", DB + ".bak-reconcile-20261001")
else:
    print("dry run; pass --apply")
con.close()
