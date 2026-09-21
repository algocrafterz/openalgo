---
name: eod
description: Run and investigate OpenAlgo's signal_engine end-of-day review for a trading day — read the eod.sh report, root-cause every FAILED check against trades.db and the broker tradebook (not guesses), fix what's provably a bug with a regression test, and give a plain-language summary. Use after market close on a trade day, or when asked to "do EOD analysis" / "review today's trading".
---

# Signal engine EOD analysis

`signal_engine/analysis/eod.sh` already runs on cron at 15:45 IST every weekday and writes
`signal_engine/analysis/reports/eod-YYYY-MM-DD.md`. This skill is the human-in-the-loop pass on
top of that report: don't just read the numbers back, **find out why anything failed and either
fix it or explain precisely why it isn't a bug.**

## 1. Get today's report

```bash
DAY=$(date +%F)
REPORT="signal_engine/analysis/reports/eod-$DAY.md"
if [ ! -s "$REPORT" ]; then
  ./signal_engine/analysis/eod.sh "$DAY"   # cron should have run this at 15:45 IST; re-run if missing
fi
cat "$REPORT"
```

If it's a weekend/NSE holiday the script exits early with "nothing to review" — stop here, say so,
don't fabricate an analysis. If the tradebook snapshot failed ("WARNING: tradebook snapshot
failed — is OpenAlgo running?"), say so up front: every number in the report is signal-side only
until it's re-run while OpenAlgo is up.

## 2. Triage the regression check

The report's last section ("EOD regression check... posted to Telegram") has PASS/FAIL lines
grouped TRADE / SIGNAL / SYSTEM. Read every FAIL — do not skip past one just because a prior EOD
already flagged something similar. For each FAIL, go to the matching subsection below before
concluding anything.

### TRADE — reconciliation mismatch (`Engine (trades.db): X | Broker (OpenAlgo): Y`)

This is `signal_engine/reconcile.py`'s canary (`TOLERANCE_RUPEES = 1.0`) — it never lies about
*whether* the two numbers differ, but the difference has more than one possible cause. Investigate
before assuming which one, in this order:

1. **Pull both sides at the row level.**
   ```bash
   sqlite3 signal_engine/data/trades.db ".headers on" ".mode column" \
     "SELECT symbol, direction, quantity, fill_price, order_id, executed_at, context
      FROM trades WHERE trade_mode='analyze' AND date(executed_at)='$DAY' ORDER BY executed_at;"

   python3 -c "
   import json
   for t in json.load(open('signal_engine/data/tradebook/tradebook_$DAY.json')):
       print({k: t.get(k) for k in ('symbol','action','quantity','average_price','orderid')})
   "
   ```
2. **Match every trades.db row's `order_id` against the tradebook's `orderid`.** A position that
   had partial exits (TP1/TP2 before a final close) is the case to check hardest: as of
   2026-09-21, `book_close()` in `tracker.py` had a bug where the FINAL leg of a multi-leg close
   wrote `order_id=<the entry's order_id>` and `quantity=<the original full size>` instead of the
   order that actually closed it and the remaining size — making that row indistinguishable from a
   duplicate of the entry, and the real closing fill invisible to any order_id-keyed join. That
   specific bug is fixed (`tracker.py`'s `book_close()` now uses `pos.sl_order_id` /
   `pos.quantity`), but treat any *other* order_id/quantity mismatch you find the same way: reject
   the "must be a broker glitch" explanation until you've confirmed trades.db's own bookkeeping is
   internally consistent, by hand-summing entry vs every exit leg's context `pnl` field.
3. **If the row-level data reconciles but the day-total still doesn't (engine "gross" price-delta
   P&L vs broker "net" `m2mrealized`):** that gap is plausibly brokerage/STT/exchange charges on
   turnover, which the engine's own P&L math does not model at all. This is a real, structural
   limitation of `reconcile.py`'s ₹1 tolerance, not a new bug each time it fires — say so plainly,
   estimate the turnover-implied cost if the gap is roughly consistent with it, and don't re-open
   the "why doesn't this reconcile" investigation from scratch every single day once this
   explanation has been confirmed once. Flag it as a standing product question (net the estimate,
   or widen the tolerance) rather than a fresh incident.
4. Only propose a code fix once you can explain the number, not before. A plausible-sounding guess
   that isn't checked against the actual DB rows is exactly the failure mode `reconcile.py` exists
   to catch — don't reproduce it in the investigation.

### TRADE / general ledger — "broker fills the engine never sent: N"

Same root cause class as above: an order_id the ledger's join can't find on the engine side. Cross
reference the same way (step 1-2). If N equals the count of positions that had partial exits that
day, it's very likely the same order_id/quantity misattribution pattern — confirm, don't assume.

### SYSTEM — "N signal_engine errors today" / "N OpenAlgo app errors today"

Don't take the count at face value — read the actual lines and classify each one:

```bash
# signal_engine's own errors for the day
cat signal_engine/logs/errors_$DAY.jsonl 2>/dev/null | python3 -c "
import json,sys
for line in sys.stdin:
    d=json.loads(line); print(d.get('level'), d.get('message','')[:140])
"

# OpenAlgo app-wide errors for the day (main log/errors.jsonl covers the whole platform,
# not just signal_engine — filter by date and dedupe by message before reacting to the count)
python3 -c "
import json
from collections import Counter
c = Counter()
with open('log/errors.jsonl') as f:
    for line in f:
        try: d = json.loads(line)
        except Exception: continue
        if d.get('ts','').startswith('$DAY'):
            c[d.get('message','')[:100]] += 1
for msg, n in c.most_common(30): print(n, msg)
"
```

Two known-benign patterns to recognize on sight (do not re-investigate these from scratch):
- **`No auth token to verify`** from `openalgoscheduler.py` at/near midnight or right around a
  scheduler run — routine: it's checking a token before the day's login has happened. Only a
  concern if it also appears repeatedly *during* market hours (09:15-15:30 IST).
- **Exact-N-times-repeated messages** (e.g. the same synthetic broker/credential error appearing
  3x, 4x, 6x) that read like deliberately-triggered test scenarios ("Invalid credentials",
  "nonexistent_broker_xyz", "Broker changed from 'X' to 'Y'") — this is pytest's error-path tests
  writing into the same `log/errors.jsonl` the production app uses, not a live incident. Confirm
  by checking whether a test run happened that day (`git log`, shell history, or just the
  message's clear artificiality) before treating it as production noise worth chasing.

Anything that doesn't match one of those two patterns and recurs, or happened during market hours,
is worth tracing to its source file:line (the JSON record has both) and reading the surrounding
code.

### SIGNAL — notifier / BreakingTrade delivery failures

These are usually genuine — a Telegram send failing, or a scanner signal that never reached the
engine. Trace via `signal_engine_analyze_<day>.log` around the failure's timestamp; there's no
known-benign pattern to short-circuit this one.

## 3. Fix only what's confirmed

- A fix must be traceable to a specific, quoted piece of evidence (a DB row, a log line, a code
  path) — not "this looks like it could explain it."
- Keep fixes surgical: this is a live trading engine (paper/analyze mode today, but the same code
  path runs live). Match the existing style in `tracker.py`/`db.py`/`reconcile.py` — see how
  `book_close()`'s fix explains itself in a comment rather than just changing the line.
- Add a regression test in the matching `signal_engine/tests/test_*.py` file that reproduces the
  exact scenario found (see `test_close_accounting.py`'s
  `TestFinalLegAuditTrailIsNotMisattributedToTheEntry` for the pattern: real symbols/quantities/
  order_ids from the day that surfaced the bug, not synthetic placeholders).
- Run at least the affected test file, then the full suite before calling it done:
  ```bash
  PYTHONPATH=. uv run pytest signal_engine/tests/ -q
  ```
- Append a dated `## <Title> (YYYY-MM-DD)` section to `signal_engine/PRD.md` — root cause, what
  changed, what deliberately wasn't fixed and why (see the 2026-09-21 reconciliation entry for the
  shape: root cause, fix, and an explicit "not fixed, and likely not a bug" paragraph for the
  parts that turned out to be structural rather than broken).

## 4. Summarize for the user

Plain language, short: what ran clean, what was found, what was fixed (with the regression test
and full-suite result), what's still open and why it's left open (needs a product decision, needs
more days of data, etc). This is a daily habit — keep the summary skimmable, not a re-derivation
of the whole investigation each time.
