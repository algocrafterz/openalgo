---
name: execution-health
description: Read-only daily check of OpenAlgo/signal_engine execution health for one IST trading day - Flattrade rate-limit rejections, limiter waits, order timeouts, EXIT UNCERTAIN, positionbook failures, history 500s, websocket drops, stale prices, late cycles - plus how those incidents touched that day's paper trades. Use when asked for "daily execution health", "did the rate limit issue recur", "how did the API flakiness affect paper trades", or "check the 2026-10-29 fixes".
---

# Execution health

Read-only. It reads logs and `trades.db` (opened `mode=ro`) and never calls OpenAlgo, places orders,
restarts anything or edits config. It proposes; it never applies a change to trading behaviour.
Timeouts, retries, stops and cap values need the owner's approval.

## Run

```bash
uv run python -m signal_engine.analysis.execution_health --day YYYY-MM-DD --format md   # or json
```

Default day is today (IST). The cron report goes to `signal_engine/reports/execution-health-<day>.md`.
A day with no logs or trades prints a message and exits 0.

## Read the verdict

Top table: each metric has a status (OK / WATCH / REGRESSION), the previous trading day with logs,
the 2026-10-29 pre-fix baseline and the thresholds. Thresholds live in one `THRESHOLDS` block at the
top of `signal_engine/analysis/execution_health.py`, each with its reason. The cap check says whether
the `FLATTRADE_MAX_PER_SECOND=7` change is active (last limiter `cap=` string).

The paper-trade section flags a position when an incident hits its symbol and window: order
timeout at entry or exit, EXIT UNCERTAIN, a positionbook failure while held, a stale price while
held, or a place_order above 5s. Flagged versus untouched counts, P&L and R are descriptive with a
small n. Do not read them as proof an incident caused a gain or loss.

A REGRESSION line is an EXECUTION entry in the tracker (`bug-tracker` skill): `seen` on the open
entry when it is the same symptom, `add` when it is new. An OK metric counts toward verifying a
`fixed` entry only on a day its trigger (slow broker, stale feed) was present.

## First check per regression

Findings from 2026-10-29 (sandbox mode, Flattrade data feed):

- Rejections, limiter waits, peak calls/min, slow place_order, order timeouts, EXIT UNCERTAIN:
  while the websocket is stale the sandbox polls prices twice over REST (about 24 calls/min per held
  symbol against a 110/min data cap), and order price lookups queue behind it. Check websocket drops
  and the limiter `cap=` and lane values first. Lanes: DATA_RESERVE 2/s 10/min, DATA_BACKGROUND
  1/s 20/min, BACKGROUND_MAX_WAIT 3s. If the cap is not 7/s the `.env` change or restart did not take.
- Rejections of 11-14 per second with the cap at 7/s are not explained yet. Report them, do not guess.
- history_service errors (ConnectionTerminated, EOF, deque mutated): HTTP/2 shared-connection bug.
  The `APP_MODE=standalone` option is NOT applied yet; propose it, do not apply it.
- Websocket drops and long outages: the resubscribe-before-auth-ack defect needs an upstream fix.
- Stale-price warnings: the tracker marks a held price stale after 120s unchanged (log text
  "unchanged for", "treating it as unconfirmed"). A burst means a stale feed while holding.
- Positionbook failures and EXIT UNCERTAIN: check the matching positions in the impact table and
  the broker tradebook before anything else. An unconfirmed exit may leave a position unmanaged.
- Limiter summary flood (2026-10-28, about 22k lines in minutes): a looping logger, not traffic.
