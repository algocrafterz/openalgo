#!/usr/bin/env bash
# Scheduled incremental refresh of the Historify backtest store. Cron-safe, idempotent,
# weekday-only.
#
# WHY A SCRIPT RATHER THAN "REMEMBER TO RUN THE BACKFILL"
# `signal_engine.backtest.backfill` with no arguments (extend_existing) only brings
# symbols already in db/historify.duckdb up to today - it does not touch coverage or
# history depth. Left unscheduled, the store just gets stale (measured 17 days stale
# on 2026-09-28) and every backtest run against it quietly reports on an older market
# than the one it prints.
#
# TIMING WINDOW
# Two constraints bound this to a narrow slot:
#   - NSE cash/F&O underlyings close at 15:30 IST. Run any earlier and the day's last
#     bars (up to 15:30) do not exist yet, so extend_existing marks the symbol "caught
#     up" on an incomplete session - a gap that silently never gets backfilled once
#     stale_days resets to 0.
#   - backfill.py reads OpenAlgo's own live broker session straight from db/openalgo.db
#     (_broker_session) - it needs OpenAlgo up and logged in, not a separate
#     secondary-broker login. OpenAlgo's actual AutoStop task runs at 16:00 IST (Windows
#     Scheduled Task, $stopTime in createTaskOpenAlgoScheduler.ps1 - NOT the 15:30 some
#     older comments in this repo assume).
# That leaves 15:30-16:00. Scheduled at 15:35 for a 5-minute buffer after close (in case
# the broker takes a moment to finalize/publish the last bar) and a 25-minute buffer
# before AutoStop - 214 symbols x 2 intervals, mostly skipped by extend_existing's
# min_stale=2 gate on any given day, finishes in well under a minute of API calls.
#
# USAGE
#   ./signal_engine/backtest/backfill_cron.sh
#
# CRON (weekdays, 15:35 IST - after the 15:30 close, before the 16:00 AutoStop):
#   35 15 * * 1-5 /home/anand/github/openalgo/signal_engine/backtest/backfill_cron.sh >> /home/anand/github/openalgo/signal_engine/logs/backfill_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

LOGS="$REPO/signal_engine/logs"
LOCK="$LOGS/.backfill.lock"
mkdir -p "$LOGS"

DAY="$(date +%F)"
DOW="$(date +%u)"
if [ "$DOW" -gt 5 ]; then
  echo "$DAY is a weekend - nothing to backfill."
  exit 0
fi

# Overlap guard, same rationale as eod.sh: a hand-run and the cron run must never
# write db/historify.duckdb at the same time, and a genuinely hung process should not
# wedge every future run either.
STALE_LOCK_SECONDS=$((2 * 3600))
if [ -f "$LOCK" ]; then
  lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || echo 0) ))
  if [ "$lock_age" -gt "$STALE_LOCK_SECONDS" ]; then
    echo "WARNING: $LOCK is ${lock_age}s old (> ${STALE_LOCK_SECONDS}s) - treating as abandoned, not a live run. Removing."
    rm -f "$LOCK"
  fi
fi
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "FATAL: another backfill run is in progress ($LOCK)"
  exit 1
fi

UV="$(command -v uv || echo /usr/local/bin/uv)"
run() { PYTHONPATH="$REPO" "$UV" run python "$@"; }

echo "=== backfill $DAY ==="
status=0
for interval in 1m D; do
  echo "--- extend_existing interval=$interval ---"
  if ! run -m signal_engine.backtest.backfill --interval "$interval"; then
    echo "WARNING: $interval extend failed - see output above (broker session down?)"
    status=1
  fi
done

exit "$status"
