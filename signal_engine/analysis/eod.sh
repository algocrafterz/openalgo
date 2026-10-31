#!/usr/bin/env bash
# End-of-day analysis for the signal engine. Cron-safe, idempotent, weekday-only.
#
# Runs after the close, while OpenAlgo is still up, and writes one dated markdown report:
#   signal_engine/reports/eod-YYYY-MM-DD.md
#
# WHY A SCRIPT RATHER THAN "REMEMBER TO RUN THE CLI"
# The paper week is only worth anything if every session is reviewed. A step that depends on
# remembering gets skipped on exactly the busy days worth reviewing most.
#
# ORDER MATTERS. The broker tradebook snapshot must happen while OpenAlgo is running: it is
# the only source of actual FILL prices, and the ledger's slippage columns are empty without
# it. A missing snapshot is reported loudly rather than silently producing a report whose
# numbers are all signal-side.
#
# USAGE
#   ./signal_engine/analysis/eod.sh            # today
#   ./signal_engine/analysis/eod.sh 2026-09-08 # a specific session
#
# SCHEDULE: run by signal_engine/analysis/close_day.sh (cron 15:36 IST, after the engine stops at
# 15:35 and before OpenAlgo stops at 16:00); no cron line of its own.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

DAY="${1:-$(date +%F)}"
REPORTS="$REPO/signal_engine/reports"
LOGS="$REPO/signal_engine/logs"
REPORT="$REPORTS/eod-$DAY.md"
LOCK="$LOGS/.eod.lock"
mkdir -p "$REPORTS" "$LOGS"

# Weekday guard. Cron's 1-5 already covers it, but the script is also run by hand and an
# NSE holiday still produces an empty report that looks like a bad day rather than no day.
DOW="$(date -d "$DAY" +%u 2>/dev/null || echo 1)"
if [ "$DOW" -gt 5 ]; then
  echo "$DAY is a weekend — nothing to review."
  exit 0
fi

# Overlap guard: a hand-run and the cron run must not write the same file at once. flock only
# releases when the holding process's fd closes - a genuinely HUNG process (not a stale
# pidfile) keeps holding it indefinitely, and every day's cron run after that fails instantly
# with nothing to show for it but "another EOD run is in progress". 2026-09-16 through
# 2026-09-18: exactly that - three straight trading days silently lost their broker
# verification to an unknown hung process before it cleared on its own by 2026-09-21 (see
# docs/PRD.md). A run this script performs takes seconds to at most a couple of minutes; nothing
# legitimate should ever hold this lock anywhere near STALE_LOCK_SECONDS, so a lock older than
# that is abandoned, not active - take the lock over from it.
#
# The age test runs only when the lock is actually HELD. The file itself outlives every run
# (flock never deletes it), so testing the file's age alone flagged yesterday's finished run
# as "abandoned" every day.
STALE_LOCK_SECONDS=$((2 * 3600))
lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
exec 9>>"$LOCK"
if ! flock -n 9; then
  if [ "$lock_age" -le "$STALE_LOCK_SECONDS" ]; then
    echo "FATAL: another EOD run is in progress ($LOCK)"
    exit 1
  fi
  echo "WARNING: $LOCK held for ${lock_age}s (> ${STALE_LOCK_SECONDS}s) - treating the holder as hung. Taking over."
  exec 9>&-
  rm -f "$LOCK"
  exec 9>>"$LOCK"
  flock -n 9 || { echo "FATAL: cannot take over $LOCK"; exit 1; }
fi
touch "$LOCK"

UV="$(command -v uv || echo /usr/local/bin/uv)"
run() { PYTHONPATH="$REPO" "$UV" run python "$@"; }

echo "=== EOD $DAY ==="
snapshot_rc=0
run -m signal_engine.analysis --snapshot --day "$DAY" || snapshot_rc=$?
if [ "$snapshot_rc" -eq 3 ]; then
  echo "WARNING: tradebook snapshot saved, but some fills had no readable time and were set aside"
  echo "         in unverified_tradebook_$DAY.json. They are not in this report; check their dates."
elif [ "$snapshot_rc" -ne 0 ]; then
  echo "WARNING: tradebook snapshot failed — is OpenAlgo running? Fill prices and slippage"
  echo "         will be missing from this report. Re-run before OpenAlgo is stopped."
fi

{
  echo "# EOD review — $DAY"
  echo
  echo "Generated $(date '+%Y-%m-%d %H:%M %Z') by \`signal_engine/analysis/eod.sh\`."
  if [ "$snapshot_rc" -eq 3 ]; then
    echo
    echo "> **Broker tradebook snapshot INCOMPLETE.** Fills with no readable time were set aside in"
    echo "> \`signal_engine/data/tradebook/unverified_tradebook_$DAY.json\` and are missing below."
  elif [ "$snapshot_rc" -ne 0 ]; then
    echo
    echo "> **Broker tradebook snapshot FAILED.** Every number below is signal-side only:"
    echo "> no fill prices, no slippage, no NO_FILL detection. Re-run this script while"
    echo "> OpenAlgo is up to replace this report."
  fi
  echo
  echo '## Ledger and declined signals'
  echo
  echo '```'
  run -m signal_engine.analysis --since "$DAY" --positions --declined 2>&1
  echo '```'

  if [ -f "$REPO/signal_engine/analysis/breakingtrade/review.py" ]; then
    echo
    echo '## BreakingTrade scanner vs engine'
    echo
    echo '```'
    run -m signal_engine.analysis.breakingtrade --review --day "$DAY" 2>&1 || echo "(review unavailable)"
    echo '```'
  fi

  echo
  # Errors for the day are the close-day `logs` step (signal_engine/ops/logscan.py).
  echo
  echo '## Strategy P&L / Performance pages vs broker fills'
  echo
  echo '```'
  run -m signal_engine.analysis.pages_vs_fills "$DAY" 2>&1 || echo "(MISMATCH: a page figure is not backed by a fill - see above)"
  echo '```'

  echo
  echo '## EOD regression check (trade / signal / system)'
  echo
  echo '```'
  # Only the same-day run posts to Telegram; a re-run of an earlier day prints the digest instead
  # of announcing an old session as today's.
  POST_FLAG=""
  { [ "$DAY" = "$(date +%F)" ] && [ "${EOD_TELEGRAM:-1}" = "1" ]; } || POST_FLAG="--dry-run"
  run -m signal_engine.analysis.eod_review "$DAY" $POST_FLAG 2>&1 || echo "(eod_review exited non-zero — see CHECK results above)"
  echo '```'
} > "$REPORT"

echo "report -> $REPORT"
[ "$snapshot_rc" -eq 0 ] || exit 2
