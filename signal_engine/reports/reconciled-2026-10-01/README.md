# Reconciled ledger: BREAKOUT vs BREAKOUT-PY, 2026-10-01 (paper)

Interim as of 13:20 IST. After 15:30 IST re-run `python3 signal_engine/reports/reconciled-2026-10-01/build_ledger.py`
and `fix_strategy_book.py` (both from the repo root) so anything that settles after the market closes is final.

Source of truth: the sandbox's own fills (`db/sandbox.db`, tagged by strategy). `trades.db` is only a cross-check.
Neither was modified by the ledger. `ledger.csv` has one row per trade.

| Status | Meaning |
|---|---|
| VERIFIED | sandbox fills and engine P&L agree within 1 rupee |
| CORRECTED | engine P&L was wrong or missing; ledger uses real fills (see note per row) |
| GUARD_ABORT | entry guard closed it on slippage; a real paper trade, counted |
| EXCLUDED_ARTIFACT | stray order from an engine bug, not a trade |

## Totals (VERIFIED + CORRECTED + GUARD_ABORT)

| | Trades | Wins | Real P&L (Rs) | What the engine booked for the same trades |
|---|---|---|---|---|
| BREAKOUT (Pine) | 9 | 3 | +2,626 | +1,865 on the rows it booked (1 trade had no row) |
| BREAKOUT-PY | 19 | 9 | +3,113 | +5,682 on the rows it booked (2 trades had no row) |

These totals equal the Strategy P&L and Performance pages exactly (those read core's fill-based `strategy_closed_trades`).

## Why the engine's own numbers were off (fixed in main.py / tracker.py)

- Exit P&L was the whole account's realised delta since the last booking, so it absorbed other strategies' closes
  (PY RELIANCE trade 2: booked +1,871 vs +1,362 real).
- The booked exit price was the alert's target price, not the market fill (PY RELIANCE trade 1: 1,228.8 vs 1,152.0).
- Position-book ltp, used for stop closes, is per symbol and the broker nets across strategies.
- Time exits split the account delta equally across positions.
Now every close is priced from its own order's confirmed fill, with the old delta only as a logged fallback.

## What this day supports

- Per-strategy results: yes, every counted trade is backed by real fills.
- Pine vs PY on identical signals: still no. Pine took 8 signals and PY 18; only TCS, RELIANCE and TATAPOWER overlap.
- ADANIPORTS trade 2 is the one judgement call: counted as a stop-out (-1,008.9), per the Ladder's own stop log and the
  stop fill at 1768.4, not the +153.9 safety-net close.
