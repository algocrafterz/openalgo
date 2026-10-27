# Paper session observations, 2026-10-27 (IST)

Scope: BREAKOUT-PY and EVWAP-EARLY-PY (ANALYZE mode, sandbox capital 100,000), plus the BREAKINGTRADE paper signal.
Source: `signal_engine/logs/signal_engine_analyze_2026-10-27.log`, `log/errors.jsonl`, `signal_engine/logs/backfill_cron.log`.
Checked at about 12:30 IST. Read-only review; no code or config was changed.

## 1. Startup

- OpenAlgo (5000, 8765, 5555) and signal_engine up since 08:50 / 08:51. Flattrade session resumed 08:57 with the live feed preserved.
- Mode guard armed for ANALYZE. All four Telegram live channels disabled.
- Runner warm-up: 40 ok, 0 failed, for both runners.

## 2. Previous-session data (fix 52c9d84e2)

- The warm-up rejects a symbol if its newest stored session is not the calendar's previous session (here 2026-10-23), if that session is incomplete, or if it has no bars.
- Result today: no rejections, no CRITICAL lines, no `runner_warmup_rejected` alert.
- Nightly backfill (10-26 18:49) reported the daily store behind for 2 symbols. NIFTY was fixed by a manual backfill at 19:00. M&M was not confirmed as fixed, but passed the freshness check this morning.
- Not done: the actual value-area / previous-session numbers were not recomputed by hand.

## 3. Execution summary

- 66 pipeline cycles: 64 fires, 64 submitted, 6 skipped as late (11:55 and 12:00 cycles).
- Entry fills within about 1 rupee of signal price. Stops placed after each entry. Exit P&L taken from the order's own fill.
- Slowest cycle 105.8 s (normal is 17 to 40 s).

## 4. Issues observed

| # | Observation | Effect | Status |
|---|---|---|---|
| A | Broker data feed unstable: "Server disconnected", HTTP 500 and ReadTimeout on history calls, about 8 Flattrade WebSocket drops | Slow cycles, 6 late skips | Open, external |
| B | Entry order send timeouts: CHOLAFIN, AXISBANK (x2), INFY, HDFCBANK, SUNPHARMA | CHOLAFIN and AXISBANK: engine found the order had filled, recovered the position, placed the stop. No duplicate. INFY, HDFCBANK, SUNPHARMA outcome not traced | Partly verified |
| C | Stop cancel before exit failed: 7 x HTTP 400, 3 hard failures (TCS, HCLTECH x2) | TCS: no-progress exit did not happen because the stop stayed working ("trigger pending"), no exit placed. HCLTECH: trail exit went out at 12:05 | Open. Cause unconfirmed (suspect: stop already triggered in sandbox) |
| D | TCS (EVWAP-EARLY-PY, no-TP strategy) hit the tracker "No-progress" gate showing tp=2405.85 | Possible rule mismatch with a no-TP trailing strategy | Needs owner decision, trading behaviour, not changed |
| E | Non-blocking noise: NIFTYFPI Yahoo 404, master-contract cache hook `'NoneType' ... 'emit'` at 08:50 | None seen | Ignore unless it recurs |

## 5. Live-mode comparison plan

Run when a strategy is promoted. Compare each item against this paper baseline. Paper uses the OpenAlgo sandbox, so A to C may behave differently against the real broker.

1. Warm-up: confirm 40 ok / 0 failed, no `runner_warmup_rejected`, and the logged previous session equals the calendar's previous session.
2. Order timeouts (B): after any "Timeout sending order", confirm the recovery path finds the real broker order, reuses its id, and places exactly one stop. Check the broker order book for duplicates.
3. Stop cancel (C): for every exit, confirm cancel-stop succeeded before the exit order. On any HTTP 400, record the stop's broker status at that moment. Verify no position is left without a stop and no exit is left unplaced.
4. No-progress gate (D): after the owner decides the intended rule, verify live behaviour matches it.
5. Feed (A): compare late-skip count and cycle duration against paper (6 late, worst 106 s). Check the live feed reconnects without losing subscriptions.
6. Fills: compare entry slippage and exit fill against paper (about 1 rupee on entries). Reconcile engine P&L to the broker tradebook at EOD.
7. Risk: paper ran with no position cap (24 positions open at one point). Confirm live limits (max positions, daily loss) apply as configured.

## 6. Open items

- Decide D (no-progress gate on trailing no-TP strategies).
- Trace B outcomes for INFY, HDFCBANK, SUNPHARMA.
- Find the cause of C.
- Confirm M&M daily data after the next nightly backfill.
