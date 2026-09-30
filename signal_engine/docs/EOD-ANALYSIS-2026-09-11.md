# EOD Analysis — 2026-09-11

First trading day reviewed end to end against the day's own logs, `trades.db`,
`breakingtrade.db` and OpenAlgo's `logs.db` traffic table.

**Read this first:** the engine that ran today started on **10 Sep** and was never restarted
into the code reviewed that night. Everything below is the *pre-fix* system. None of the
2026-09-11 commits were live. That is also why the log shows the old channel names
(`[intraday-breakout]`, not `[intraday-breakingtrade-analyze]`).

---

## What actually happened

| | |
| --- | --- |
| BreakingTrade signals validated | 26 |
| Entered | **4** (ADANIENSOL, ADANIENT, AXISBANK, CROMPTON) |
| Refused for sandbox margin | **14** |
| Ignored for R:R below 0.75 | 8 |
| ORB | 1 declined, 1 broker-rejected, 0 entered |
| BREAKOUT | 0 entered — but a TP1 HIT arrived at 14:00 for a position it never opened |
| BREAKINGTRADE-WATCHLIST | 0 — never reached the engine |
| All 4 positions closed by | the **no-progress gate**, 13:16 and 13:35 |
| Day summaries sent | BreakingTrade only |

Reported day P&L was **+₹986.41**. The broker's own per-symbol figures make it roughly
**−₹78**. Every number in today's paper result is wrong, and the reasons are below.

---

## The ten defects

### 1. P&L was attributed from a portfolio-level delta (CRITICAL)

`_book_broker_close()` computed `pnl_delta = fetch_realised_pnl() - _last_realised_pnl` — the
whole **account's** realised P&L. Two positions closing in the same poll cycle meant the first
booked absorbed the entire delta:

```
13:16:53  ADANIENSOL  booked +897.40     broker's own figure: -197.20
13:16:53  ADANIENT    booked   +0.00
```

The day summary reported a ₹897 winner that was a ₹197 loser. The positionbook has carried the
per-symbol figure all along — reconciliation already reads it.

**Fixed:** `_index_positionbook()` returns a `BookEntry(quantity, ltp, realised)` and the close
path uses the broker's per-symbol number, falling back to the portfolio delta only when the
broker reports none.

### 2. Tracker-detected closes never reached `trades.db` (CRITICAL)

`book_close()` filed an in-memory record, advanced the day counters and sent Telegram — and
wrote **no EXIT row**. `trades.db` is what every performance report and the ledger read. So all
four closes left no audit trail, the 15:05 restart found "no EXIT recorded", and reconciliation
booked them a **second** time — calling `record_close()` twice per position with different P&L.

**Fixed:** new `db.save_tracker_exit()`, called from `book_close()`. Never raises: the position
is closed at the broker either way.

### 3. The TP ladder could never advance, and re-fired TP1 forever (CRITICAL)

`tp_watch._last_level_hit()` filtered with `created_at >= executed_at` as a **string**
comparison across two databases that format timestamps differently:

```
trades.executed_at  '2026-09-11T11:46:43.327851'   <- isoformat(), 'T'
alerts.created_at   '2026-09-11 14:52:16'          <- space
                               ^ index 10: ' ' (0x20) sorts BELOW 'T' (0x54)
```

A *later* alert always compared as smaller, so the filter matched nothing, the function always
returned `None`, and `_next_level(None)` was always `"TP1"`. AXISBANK got **five TP1 alerts**
between 14:52 and 14:54. Each carries `ExitQtyPct: 50`, so against a live position that is half
the remainder exited, five times over. Structurally incapable of working since it was written —
the same shape as the `fetch_bars` UTC bug.

Today it was masked only because the position had already closed, so all five were answered
"no open position".

**Fixed:** `datetime()` on both sides.

### 4. Every BreakingTrade alert overstated its R:R by exactly 2.00x — all 25

Sent `TP: targets[0]` (1.0x IB) while the `R:R:` line used `targets[-1]` (2.0x). The ladder is
computed but not wired through, so the advertised figure described an exit the engine never
performs. UNIONBANK showed **1:1.2** in the channel and was then IGNORED by the engine for
falling under `min_rr: 0.75`, at its real **1:0.60**.

**Fixed** (earlier today): R:R measures the TP actually sent; the ladder is reported separately
as `Runner target (not traded yet)`.

### 5. 14 of 26 signals refused for sandbox margin, recorded as broker errors

The sandbox drained to ₹3,073 while the engine sized every trade off the ₹1,00,000
`sandbox_capital` override. All three guards read the override, so `min_capital_for_entry`
compared ₹1,00,000 against its ₹5,000 floor and passed, and the margin check is skipped in
analyze mode anyway. A **capacity** limit was recorded as a **broker** problem.

**Fixed:** `fetch_funds_available()` reads the real balance; `_sandbox_can_fund()` declines with
`stage="sandbox_margin"`. **You must also top the sandbox up** — see the plan.

### 6. Exits were all labelled "SL". None was an SL hit

All four were no-progress market exits. The label reaches the day summary and the trade record,
so the exit-type mix for the week is wrong.

**Fixed:** the tracker only assumes SL when no other path has claimed the exit.

### 7. Two channels got no EOD summary at all

`_send_per_strategy_day_summaries()` iterated only strategies with a **closed trade**, so
intraday-orb and intraday-breakout stayed silent — indistinguishable from "the engine was
down", which is exactly the ambiguity `enabled: false` was introduced to end.

**Fixed:** every enabled channel for the current phase files a report, including
`No trades taken today.`

### 8. BTST counted the same stock twice

The closing scan runs at 14:50 **and** 15:10, and each run opened a separate paper position:

```
AXISBANK  entry 14:50 @ 1245.50 -> 1250.00  +0.36%
AXISBANK  entry 15:10 @ 1248.00 -> 1250.00  +0.16%
```

Both appeared in the winners list. "10 settled | 3 winners, 7 losers" described about six
distinct stocks, and the win rate was weighted by which names happened to appear in both runs.

**Fixed — and this answers "one position for one stock" directly:** a unique index on
`(strategy, symbol, date(entry_at))` now makes a second same-day position *impossible*, and the
**first recommendation of the day wins** — the price a trader acting on the first alert would
have had. Existing duplicates are collapsed on first connect (verified on a copy of the live
database: 78 rows -> 69, 9 groups collapsed, idempotent on re-run).

### 9. `Entry: 0.0 / SL: 0.0` in exit messages

Real, but not a bug: the parser's EXIT short-path synthesizes them and the engine looks the
position up in its own tracker. It reads as broken in the channel, though, and it is the first
thing you noticed. **Left as-is deliberately** — ORB and BREAKOUT's PineScript alerts use the
identical shape, and changing one side only would create two formats. Flagged in the plan as a
cosmetic change to make on both sides at once, or not at all.

### 10. Orphaned TP HITs from strategies that never entered

BREAKOUT's TP1 HIT for INDUSINDBK at 14:00 found no position. Harmless today, but it means the
channel shows exits for trades that never happened — noise that will mislead the weekly review.
Root cause is #5: the entry was refused, the PineScript does not know that, and it alerts on its
own schedule regardless. Nothing to fix in the engine; it resolves once the sandbox can fund
entries.

---

## Does analyze mirror live?

Not yet — but the gap is now understood, and it is mostly these ten defects rather than anything
structural. What differs by design, and what was accidental:

| Difference | Status |
| --- | --- |
| P&L attribution | **was broken in both modes** — fixed |
| Audit trail completeness | **was broken in both modes** — fixed |
| TP ladder progression | **was broken in both modes** — fixed |
| Margin check | skipped in analyze **by design**; now gated on the real sandbox balance instead |
| Capital | fixed `sandbox_capital` pool vs live broker funds — **by design**, but the pool must be fundable |
| Slippage | sandbox fills at the requested price; live does not — **irreducible**, and the single biggest remaining difference |

Defects 1, 2, 3 and 6 were **not** analyze-only. They would have behaved identically with real
money — #3 in particular would have repeatedly exited half a live position.

---

## Plan

### Before the next session (you)

1. **Top up the sandbox.** At ₹3,073 the engine cannot fund a single position, so tomorrow
   repeats today. `sizing.sandbox_capital` is ₹1,00,000, so the sandbox account should hold at
   least that. Until it does, the new gate will decline cleanly and say so — correct, but it
   still produces no sample.
2. **Restart the stack.** Today's engine predates every fix. Nothing above takes effect until
   `openalgoctl.sh restart`.
3. **Re-point the TradingView alerts** for ORB and BREAKOUT at their `-analyze` channel ids if
   they are not already — the phase gate now refuses a mismatch instead of trading it.

### Already in place (this session)

- A startup config check reports duplicate channel names/ids, missing `-analyze`/`-live` twins
  and both-phases-enabled, every session.
- Six consecutive positionbook failures raise a CRITICAL alert naming how many positions are
  unmanaged, with a recovery notice.
- A fill-price read that fails warns once with the consequence spelled out.
- The sizing line carries implied leverage and, when margin scaling moved the quantity, the
  actual risk taken versus the intended 1%.

### Worth doing next (not blocking)

- **Wire the staged TP ladder through, or remove it.** It is computed, advertised (until
  today's fix) and never executed. Now that #3 is fixed the watcher will actually advance
  TP1 -> TP1.5 -> TP2, so this is the moment to decide whether that is what you want.
- **Reconcile the first hour after a restart.** Today's 15:05 restart re-booked four closes.
  Fix #2 prevents the cause, but a restart-time check that a reconciled exit is not
  double-counting an existing one would make it impossible.
- **Give BREAKINGTRADE-WATCHLIST a real run.** It produced 10 signals today and traded none,
  because the confirmed strategy exhausted the sandbox first. With H4 (per-strategy symbol caps)
  and a funded sandbox, the head-to-head can finally run.

### The number to watch tomorrow

Day-summary P&L should now agree with the broker's per-symbol figures. If it does not, #1 is not
fully fixed and nothing else in the paper record can be trusted.
