#!/usr/bin/env bash
# Close the trading day: every after-market job, in order, finished before 16:00 IST.
#
# ONE ENTRY POINT. This replaces four separate cron lines (eod.sh 15:25, backfill_cron.sh 15:35,
# eod_claude_review.sh 15:40, execution_health 16:10) that ran on unrelated clocks: the review
# started before execution health existed for the day, and execution health ran after OpenAlgo's
# 16:00 stop. The interactive counterpart is the `close-day` skill (/close-day), which reads what
# this produced and finishes anything it could not.
#
# TIMELINE (weekdays; the Windows tasks stop the engine at 15:35 and OpenAlgo at 16:00)
#   15:36  start: the engine is stopped, so trades.db and the sandbox are final
#   15:36  backfill_cron.sh in the background (needs OpenAlgo; about 7 minutes)
#   15:36  eod.sh: tradebook snapshot, ledger, pages vs fills, regression check to Telegram (< 1 min)
#   15:37  execution health report (seconds)
#   15:37  Fridays: weekly.sh rollup for Monday..Friday (seconds)
#   15:37  Claude close-day review, report-only, bounded by the deadline (typically 5-10 minutes)
#   ~15:50 backfill joined, status written; hard deadline 15:58, before OpenAlgo stops
#
# Each step's start, end and exit code go to signal_engine/reports/close-day-<day>-status.tsv,
# so the review and /close-day can say exactly what ran and what did not.
#
# USAGE
#   ./signal_engine/analysis/close_day.sh               # today
#   ./signal_engine/analysis/close_day.sh 2026-10-30    # re-run a day (OpenAlgo must be up for
#                                                       # the snapshot and the backfill)
#   ./signal_engine/analysis/close_day.sh --no-review   # deterministic steps only
#
# CRON (weekdays, 15:36 IST):
#   36 15 * * 1-5 /home/anand/github/openalgo/signal_engine/analysis/close_day.sh >> /home/anand/github/openalgo/signal_engine/logs/close_day_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

REVIEW=1
DAY="$(date +%F)"
for arg in "$@"; do
  case "$arg" in
    --no-review) REVIEW=0 ;;
    *) DAY="$arg" ;;
  esac
done

if [ "$(date -d "$DAY" +%u)" -gt 5 ]; then
  echo "$DAY is a weekend - nothing to close."
  exit 0
fi

# NSE holiday guard, from OpenAlgo's own exchange calendar (Settings > Market calendar).
HOLIDAY="$(sqlite3 "file:$REPO/db/openalgo.db?mode=ro" "SELECT h.description FROM market_holidays h \
  JOIN market_holiday_exchanges e ON e.holiday_id = h.id \
  WHERE h.holiday_date = '$DAY' AND e.exchange_code = 'NSE' LIMIT 1" 2>/dev/null)"
if [ -n "$HOLIDAY" ]; then
  echo "$DAY is an NSE holiday ($HOLIDAY) - nothing to close."
  exit 0
fi

UV="$(command -v uv || echo /home/anand/.local/bin/uv)"  # cron has a minimal PATH
ANALYSIS="$REPO/signal_engine/analysis"
REPORTS="$REPO/signal_engine/reports"
LOGS="$REPO/signal_engine/logs"
STATUS="$REPORTS/close-day-$DAY-status.tsv"
mkdir -p "$REPORTS"

# Hard deadline: OpenAlgo stops at 16:00. A same-day run gets until 15:58; a re-run of an
# earlier day (by hand) gets 20 minutes from now.
if [ "$DAY" = "$(date +%F)" ]; then
  DEADLINE="$(date -d "$DAY 15:58" +%s)"
else
  DEADLINE="$(( $(date +%s) + ${CLOSE_DAY_BUDGET_SECONDS:-1200} ))"
fi
remaining() { local left=$(( DEADLINE - $(date +%s) )); [ "$left" -gt 30 ] && echo "$left" || echo 30; }

printf 'step\tstarted\tended\texit\n' > "$STATUS"
step() {  # step NAME CMD... : run with the deadline as its timeout, record the outcome
  local name="$1"; shift
  local started; started="$(date +%T)"
  timeout --kill-after=15 "$(remaining)" "$@"
  local rc=$?
  printf '%s\t%s\t%s\t%s\n' "$name" "$started" "$(date +%T)" "$rc" >> "$STATUS"
  [ "$rc" -eq 124 ] && echo "WARNING: $name stopped at the deadline"
  return "$rc"
}

echo "=== close day $DAY (started $(date +%T)) ==="

# The backfill only needs OpenAlgo and is the longest step: start it first, join it last.
step backfill "$REPO/signal_engine/backtest/backfill_cron.sh" >> "$LOGS/backfill_cron.log" 2>&1 &
BACKFILL_PID=$!

step eod "$ANALYSIS/eod.sh" "$DAY" >> "$LOGS/eod_cron.log" 2>&1

step execution_health "$UV" run python -m signal_engine.analysis.execution_health --day "$DAY" --format md \
  > "$REPORTS/execution-health-$DAY.md" 2>> "$LOGS/execution_health_cron.log"

if [ "$(date -d "$DAY" +%u)" -eq 5 ]; then
  step weekly "$ANALYSIS/weekly.sh" "$(date -d "$DAY -4 days" +%F)" "$DAY" >> "$LOGS/weekly_cron.log" 2>&1
fi

if [ "$REVIEW" -eq 1 ]; then
  step review "$ANALYSIS/eod_claude_review.sh" "$DAY" >> "$LOGS/eod_claude_cron.log" 2>&1
fi

wait "$BACKFILL_PID"

# The review runs while the backfill is still writing, so it cannot report tomorrow's data
# readiness; the verify lines are appended here, once the backfill has finished.
REVIEW_REPORT="$REPORTS/eod-$DAY-claude-review.md"
if [ -f "$REVIEW_REPORT.partial" ]; then
  # The deadline stopped the review (timeout kills its whole process group): keep what it wrote.
  { cat "$REVIEW_REPORT.partial"; echo; echo "> Review stopped at the 15:58 deadline: incomplete, no tracker changes applied. Run /close-day."; } > "$REVIEW_REPORT"
  rm -f "$REVIEW_REPORT.partial"
fi
if [ -f "$REVIEW_REPORT" ]; then
  {
    echo
    echo "## Data for tomorrow (appended by close_day.sh after the backfill)"
    echo
    echo '```'
    sed -n "/^=== backfill $DAY ===/,\$p" "$LOGS/backfill_cron.log" | grep -E "^verify|FAIL|ERROR|behind" | tail -8
    echo '```'
    echo
    echo "## Close-day steps"
    echo
    echo '```'
    column -t -s $'\t' "$STATUS"
    echo '```'
  } >> "$REVIEW_REPORT"
fi
echo "=== close day $DAY done $(date +%T); status -> $STATUS ==="
column -t -s $'\t' "$STATUS"
