#!/usr/bin/env bash
# Catch-up for the close-day review: run it again after the 15:36 run when that run produced no
# readable verdict (Claude usage limit, expired sign-in, deadline). Does nothing when the day's
# review already holds a verdict, so it is safe to schedule more than once.
#
# The review only reads local files (trades.db, tradebook snapshots, logs, reports), so it needs
# no OpenAlgo session. It stays report-only; the tracker is written by eod_claude_review.sh
# through the validating CLI, as in the 15:36 run.
#
# NO CRON LINE. The 18:35 and 21:05 evening lines are removed from the schedule (nothing routine
# runs after 16:30 IST). The review is retried by close_day.sh --retry (16:01) and by
# run_upkeep.sh recover (08:36 next trading day), which share the per-attempt files of
# eod_claude_review.sh. This script stays for the Daily Workflow page (signal_engine/ops/actions.py)
# and for a hand run.
#
# USAGE
#   ./signal_engine/analysis/review_catchup.sh [YYYY-MM-DD]

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

DAY="${1:-$(date +%F)}"
REPORT="$REPO/signal_engine/reports/eod-$DAY-claude-review.md"
STATUS="$REPO/signal_engine/reports/close-day-$DAY-status.tsv"

# Only a trading day whose close-day run started has anything to review.
if [ ! -f "$STATUS" ]; then
  echo "$DAY: no close-day status file (weekend, holiday, or it did not run) - nothing to catch up."
  exit 0
fi
if [ -f "$REPORT" ] && grep -q '^```close-day-verdict' "$REPORT"; then
  echo "$DAY: the review already has a verdict - nothing to catch up."
  exit 0
fi

echo "=== review catch-up $DAY $(date +%T) ==="
STARTED="$(date +%T)"
timeout --kill-after=15 1500 "$REPO/signal_engine/analysis/eod_claude_review.sh" "$DAY"
rc=$?
# The newest review row of the status file must describe this attempt, or the page keeps
# reporting the earlier attempt's exit code (2026-11-13). Same append-a-row
# convention as a step run again from the page. The manifest holds the review outcome as well.
printf '%s\t%s\t%s\t%s\n' review "$STARTED" "$(date +%T)" "$rc" >> "$STATUS"

if [ -f "$REPORT.partial" ]; then
  # The timeout stopped the review: keep what it wrote, as close_day.sh does.
  { cat "$REPORT.partial"; echo; echo "> Review stopped at the time limit: incomplete, no tracker changes applied. Run /close-day."; } > "$REPORT"
  rm -f "$REPORT.partial"
fi

PYTHONPATH="$REPO" "${PY:-$(command -v python3 || echo /usr/bin/python3)}" -m signal_engine.ops.verdict record-review --day "$DAY" || true
if [ -f "$REPORT" ] && grep -q '^```close-day-verdict' "$REPORT"; then
  echo "$DAY: the review now has a verdict (see /logs/workflow)."
else
  echo "$DAY: still no verdict (review exit $rc). Next try: the 16:01 retry or the next morning's 08:36 recovery."
fi
