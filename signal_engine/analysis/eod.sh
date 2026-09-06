#!/usr/bin/env bash
# End-of-day analysis for the signal engine. Cron-safe, idempotent, weekday-only.
#
# Runs after the close, while OpenAlgo is still up, and writes one dated markdown report:
#   signal_engine/analysis/reports/eod-YYYY-MM-DD.md
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
# CRON (weekdays, 15:45 IST — after the 15:30 close, before OpenAlgo is stopped):
#   45 15 * * 1-5 /home/anand/github/openalgo/signal_engine/analysis/eod.sh >> /home/anand/github/openalgo/signal_engine/logs/eod_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

DAY="${1:-$(date +%F)}"
REPORTS="$REPO/signal_engine/analysis/reports"
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

# Overlap guard: a hand-run and the cron run must not write the same file at once.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "FATAL: another EOD run is in progress ($LOCK)"
  exit 1
fi

UV="$(command -v uv || echo /usr/local/bin/uv)"
run() { PYTHONPATH="$REPO" "$UV" run python "$@"; }

echo "=== EOD $DAY ==="
snapshot_ok=1
if ! run -m signal_engine.analysis --snapshot; then
  snapshot_ok=0
  echo "WARNING: tradebook snapshot failed — is OpenAlgo running? Fill prices and slippage"
  echo "         will be missing from this report. Re-run before OpenAlgo is stopped."
fi

{
  echo "# EOD review — $DAY"
  echo
  echo "Generated $(date '+%Y-%m-%d %H:%M %Z') by \`signal_engine/analysis/eod.sh\`."
  if [ "$snapshot_ok" -eq 0 ]; then
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
    run -m signal_engine.analysis.breakingtrade --review 2>&1 || echo "(review unavailable)"
    echo '```'
  fi

  echo
  echo '## Errors logged today'
  echo
  echo '```'
  if [ -s "$LOGS/errors_$DAY.jsonl" ]; then
    "$UV" run python -c "
import json,sys
for line in open('$LOGS/errors_$DAY.jsonl'):
    try: r=json.loads(line)['record']
    except Exception: continue
    print(f\"{r['time']['repr'][11:19]} {r['extra'].get('symbol','-'):12} {r['message']}\")
" 2>&1
  else
    echo "none"
  fi
  echo '```'
} > "$REPORT"

echo "report -> $REPORT"
[ "$snapshot_ok" -eq 1 ] || exit 2
