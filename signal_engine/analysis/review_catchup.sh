#!/usr/bin/env bash
# Catch-up for the close-day review: run it again after the 15:36 run when that run produced no
# readable verdict (Claude usage limit, expired sign-in, deadline). Does nothing when the day's
# review already holds a verdict, so it is safe to schedule more than once.
#
# The review only reads local files (trades.db, tradebook snapshots, logs, reports), so it can
# run after OpenAlgo stops at 16:00. It stays report-only; the tracker is written by
# eod_claude_review.sh through the validating CLI, as in the 15:36 run.
#
# USAGE
#   ./signal_engine/analysis/review_catchup.sh [YYYY-MM-DD]
#
# CRON (weekdays, IST; the usage limit resets on a 5-hour window, so two tries):
#   35 18 * * 1-5 /home/anand/github/openalgo/signal_engine/analysis/review_catchup.sh >> /home/anand/github/openalgo/signal_engine/logs/review_catchup_cron.log 2>&1
#   5 21 * * 1-5 /home/anand/github/openalgo/signal_engine/analysis/review_catchup.sh >> /home/anand/github/openalgo/signal_engine/logs/review_catchup_cron.log 2>&1

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

UV="$(command -v uv || echo /home/anand/.local/bin/uv)"  # cron has a minimal PATH
echo "=== review catch-up $DAY $(date +%T) ==="
timeout --kill-after=15 1500 "$REPO/signal_engine/analysis/eod_claude_review.sh" "$DAY"
rc=$?

if [ -f "$REPORT.partial" ]; then
  # The timeout stopped the review: keep what it wrote, as close_day.sh does.
  { cat "$REPORT.partial"; echo; echo "> Review stopped at the time limit: incomplete, no tracker changes applied. Run /close-day."; } > "$REPORT"
  rm -f "$REPORT.partial"
fi

if [ -f "$REPORT" ] && grep -q '^```close-day-verdict' "$REPORT"; then
  # A real verdict now exists: send the day's corrected Telegram line.
  env PYTHONPATH=. "$UV" run python -m signal_engine.ops notify close-day --day "$DAY"
else
  echo "$DAY: still no verdict (review exit $rc). Next try is the next scheduled catch-up."
fi
