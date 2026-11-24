#!/usr/bin/env bash
# Close the trading day: every after-market job, in order, finished before 16:00 IST.
#
# ONE ENTRY POINT. This replaces four separate cron lines (eod.sh 15:25, backfill_cron.sh 15:35,
# eod_claude_review.sh 15:40, execution_health 16:10) that ran on unrelated clocks: the review
# started before execution health existed for the day, and execution health ran after OpenAlgo's
# 16:00 stop. The interactive counterpart is the `close-day` skill (/close-day), which reads what
# this produced and finishes anything it could not.
#
# TIMELINE (weekdays. The 15:35 Windows task stops only the BreakingTrade poller. The signal engine
# stops together with OpenAlgo, at the openAlgoAutoStop task: 16:00 today, 16:31 once the
# maintainer installs the Windows task change. By 15:36 the 14:45 time exit and the 14:55
# square-off are done, so the day's trades are normally final.)
#   15:36  start
#   15:36  backfill   backfill_cron.sh in the background (needs OpenAlgo; about 7 minutes)
#   15:36  eod        eod.sh: tradebook snapshot, ledger, pages vs fills, regression check (< 1 min)
#   15:37  execution_health, logs (error signatures), Fridays weekly (seconds each)
#   ~15:43 backfill joined; value_areas (the day's VAH/POC/VAL rows); jobs (scheduled-job audit)
#   ~15:43 track      the day's FAIL checks recorded in the bug tracker (plain code, seconds)
#   ~15:43 review     Claude close-day review, report-only, reads every output above (2-10 minutes);
#                     hard deadline 15:58. Nothing is sent to Telegram: the result is on /logs/workflow.
#   16:01  --retry    only the steps still open (see RETRY below); ends by 16:25
#   16:28  the close dead-man (run_upkeep.sh deadman close) judges the day after the retry
#
# The morning phases (open-day 09:12, liveness 09:27) are signal_engine/ops/run_phase.sh.
#
# Each step's start, end and exit code go to signal_engine/reports/close-day-<day>-status.tsv,
# so the review and /close-day can say exactly what ran and what did not.
#
# Run manifest: signal_engine/reports/close-day-<day>-run.json. State running at the start, a row
# per step, then a terminal state: completed, failed, timed_out (a step hit the 15:58 deadline) or
# interrupted (SIGTERM/SIGINT, or the script ended early). A killed run (SIGKILL, power loss) keeps
# state running; readers see a dead pid or an old start time and read it as interrupted. The
# manifest is evidence only: a failed write never stops the close.
#
# RETRY (--retry, cron 16:01): reads this run's manifest and status rows, runs again ONLY the steps
# whose newest result is not a success, in dependency order, and appends its rows. The first
# attempt is never changed. A step counts as recovered only when its real output checks out. The
# wall clock ends the run at 16:25 IST. Logic and rules: signal_engine/ops/recovery.py. The retry
# never places an order. Today only; an earlier day is recovered by run_upkeep.sh recover.
# STRICT END: the whole retry, its child termination and its last writes included, ends by 16:27:00
# IST. An outer `timeout` sends TERM at 16:26:40 and KILL 15 s later (16:26:55, a 5 s margin), so
# the 16:28 dead-man has a clear window and nothing runs near the 16:30 hard limit. A retry that
# starts with less than 30 s before 16:25 runs no step and records "started too late". The times
# are defined once, in signal_engine/ops/model.py (RETRY_*); this script asks
# `model.py retry-limits` and holds no copy of them.
# ROLLOUT ORDER: (1) merge the code; (2) install the Windows task changes (AutoStart 08:30,
# AutoStop 16:31) on a weekend: this retry needs OpenAlgo up until 16:25; (3) then install the
# cron line below; (4) then set jobs.REQUIRE_NEW_SCHEDULE = True. Cron before the Windows tasks
# makes this retry fail every day, because OpenAlgo stops at 16:00 until the task is changed.
#
# USAGE
#   ./signal_engine/analysis/close_day.sh               # today
#   ./signal_engine/analysis/close_day.sh 2026-10-30    # re-run a day (OpenAlgo must be up for
#                                                       # the snapshot and the backfill)
#   ./signal_engine/analysis/close_day.sh --no-review   # deterministic steps only
#   ./signal_engine/analysis/close_day.sh --retry       # only the open steps of today (cron 16:01)
#
# CRON (weekdays, IST):
#   36 15 * * 1-5 /home/anand/github/openalgo/signal_engine/analysis/close_day.sh >> /home/anand/github/openalgo/signal_engine/logs/close_day_cron.log 2>&1
#   1 16 * * 1-5 /home/anand/github/openalgo/signal_engine/analysis/close_day.sh --retry >> /home/anand/github/openalgo/signal_engine/logs/close_day_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

REVIEW=1
RETRY=0
DAY="$(date +%F)"
for arg in "$@"; do
  case "$arg" in
    --no-review) REVIEW=0 ;;
    --retry) RETRY=1 ;;
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
if [ "$RETRY" -eq 1 ]; then
  # Before anything below: the retry must not truncate the status file or restart the manifest of
  # the 15:36 run. It appends rows to both (signal_engine/ops/recovery.py).
  # The outer timeout comes from the wall clock, worked out by model.py (stdlib only; the one
  # definition of the retry times, shared with recovery.py): TERM at 16:26:40, KILL 15 s later.
  # A manual run for a past day gets 120 s (python exits at once).
  PY="$(command -v python3 || echo /usr/bin/python3)"
  LIMITS="$("$PY" "$REPO/signal_engine/ops/model.py" retry-limits --day "$DAY")" || LIMITS=""
  case "$LIMITS" in
    refuse)
      echo "The retry window of today is over - not retrying. Next: open the Daily Workflow page and run the open steps by hand."
      exit 0 ;;
    [0-9]*" "[0-9]*) ;;
    *)
      echo "The retry time limit could not be worked out - not retrying. Next: open the Daily Workflow page and run the open steps by hand."
      exit 1 ;;
  esac
  exec env PYTHONPATH=. timeout --kill-after="${LIMITS#* }" "${LIMITS% *}" \
    "$UV" run python -m signal_engine.ops close-day-retry --day "$DAY"
fi

ANALYSIS="$REPO/signal_engine/analysis"
REPORTS="$REPO/signal_engine/reports"
LOGS="$REPO/signal_engine/logs"
STATUS="$REPORTS/close-day-$DAY-status.tsv"
mkdir -p "$REPORTS"

# Hard deadline: OpenAlgo stops at 16:00 today (16:31 after the task change). A same-day run gets until 15:58; a re-run of an
# earlier day (by hand) gets 20 minutes from now.
if [ "$DAY" = "$(date +%F)" ]; then
  DEADLINE="$(date -d "$DAY 15:58" +%s)"
else
  DEADLINE="$(( $(date +%s) + ${CLOSE_DAY_BUDGET_SECONDS:-1200} ))"
fi
remaining() { local left=$(( DEADLINE - $(date +%s) )); [ "$left" -gt 30 ] && echo "$left" || echo 30; }

printf 'step\tstarted\tended\texit\n' > "$STATUS"

# Run manifest (stdlib python, no uv). Never fatal. Several steps write it at once (the backfill
# runs in the background), so the writer holds a lock and replaces the file atomically.
PY="$(command -v python3 || echo /usr/bin/python3)"
mf() { "$PY" "$REPO/signal_engine/ops/model.py" manifest "$1" --phase close-day --day "$DAY" "${@:2}" || true; }
FINISHED=0
STEP_PIDS=""
on_signal() {  # SIGTERM or SIGINT: stop the running steps, record the interruption, leave
  for pid in $STEP_PIDS; do kill -TERM "$pid" 2>/dev/null; done
  if [ -n "${BACKFILL_PID:-}" ]; then
    pkill -TERM -P "$BACKFILL_PID" 2>/dev/null  # the backfill step's own timeout process
    kill -TERM "$BACKFILL_PID" 2>/dev/null
  fi
  [ "$FINISHED" -eq 0 ] && mf finish --state interrupted --note "stopped by signal $1"
  FINISHED=1
  exit $(( 128 + $2 ))
}
on_exit() {  # any other way out is also an interruption, never a success
  [ "$FINISHED" -eq 0 ] && mf finish --state interrupted --note "the script ended before its last step"
}
trap 'on_signal TERM 15' TERM
trap 'on_signal INT 2' INT
trap on_exit EXIT
mf start --pid $$

step() {  # step NAME CMD... : run with the deadline as its timeout, record the outcome
  local name="$1"; shift
  local started; started="$(date +%T)"
  timeout --kill-after=15 "$(remaining)" "$@" &  # background + wait, so a signal is handled at once
  local child=$!
  STEP_PIDS="$STEP_PIDS $child"
  wait "$child"
  local rc=$?
  local ended; ended="$(date +%T)"
  printf '%s\t%s\t%s\t%s\n' "$name" "$started" "$ended" "$rc" >> "$STATUS"
  mf step --name "$name" --started "$started" --ended "$ended" --exit "$rc"
  [ "$rc" -eq 124 ] && echo "WARNING: $name stopped at the deadline"
  return "$rc"
}

echo "=== close day $DAY (started $(date +%T)) ==="

# One duty per step, one output per step. The review interprets the outputs; it never re-runs a
# step. No step sends anything to Telegram.
OPS=(env PYTHONPATH=. "$UV" run python -m signal_engine.ops)

# The backfill only needs OpenAlgo and is the longest step: start it first, join it before the
# review so the review can read its verify result.
step backfill "$REPO/signal_engine/backtest/backfill_cron.sh" >> "$LOGS/backfill_cron.log" 2>&1 &
BACKFILL_PID=$!

# The regression digest goes into the report only.
step eod "$ANALYSIS/eod.sh" "$DAY" >> "$LOGS/eod_cron.log" 2>&1

step execution_health "$UV" run python -m signal_engine.analysis.execution_health --day "$DAY" --format md \
  > "$REPORTS/execution-health-$DAY.md" 2>> "$LOGS/execution_health_cron.log"

step logs "${OPS[@]}" logs --day "$DAY" >> "$LOGS/ops_cron.log" 2>&1

if [ "$(date -d "$DAY" +%u)" -eq 5 ]; then
  step weekly "$ANALYSIS/weekly.sh" "$(date -d "$DAY -4 days" +%F)" "$DAY" >> "$LOGS/weekly_cron.log" 2>&1
fi

wait "$BACKFILL_PID"

# Needs the session's 1-minute bars, so after the backfill; reads Historify only.
step value_areas "${OPS[@]}" value-areas --day "$DAY" >> "$LOGS/ops_cron.log" 2>&1

step jobs "${OPS[@]}" jobs --day "$DAY" >> "$LOGS/ops_cron.log" 2>&1

# Record the day's failed checks in the bug tracker in plain code, before the AI review, so a
# review that cannot run (usage limit, expired sign-in, deadline) loses no finding.
step track "${OPS[@]}" track --day "$DAY" >> "$LOGS/ops_cron.log" 2>&1

# The ONE scheduled place for the log-candidate scan, after the day's findings are tracked. The
# log scan is always a dry run (no --write): its candidates go to a dated, git-ignored report for
# the owner to review; enabling `--logs --write` stays a manual decision. Deliberately not a
# step(): a failure here is logged and never changes the close-day status, the manifest or the
# retry. The 120 s cap keeps it from holding up the review.
if ! timeout --kill-after=10 120 "${OPS[@]}" track --day "$DAY" --logs \
  > "$REPORTS/logscan-candidates-$DAY.txt" 2>> "$LOGS/ops_cron.log"; then
  echo "WARNING: log scan failed for $DAY (close-day not affected)" >> "$LOGS/ops_cron.log"
fi

REVIEW_REPORT="$REPORTS/eod-$DAY-claude-review.md"
if [ "$REVIEW" -eq 1 ]; then
  step review "$ANALYSIS/eod_claude_review.sh" "$DAY" >> "$LOGS/eod_claude_cron.log" 2>&1
  if [ -f "$REVIEW_REPORT.partial" ]; then
    # The deadline stopped the review (timeout kills its whole process group): keep what it wrote.
    { cat "$REVIEW_REPORT.partial"; echo; echo "> Review stopped at the 15:58 deadline: incomplete, no tracker changes applied. Run /close-day."; } > "$REVIEW_REPORT"
    rm -f "$REVIEW_REPORT.partial"
  fi
  # Store what the review achieved (verdict, no verdict, limit or sign-in, failure), read from its
  # report; its exit code is kept as diagnostic evidence only.
  PYTHONPATH="$REPO" "$PY" -m signal_engine.ops.verdict record-review --day "$DAY" >> "$LOGS/ops_cron.log" 2>&1 || true
fi

# Fridays, same-day runs only: the weekly strategy performance report (paper and live, shown
# separately) into reports/strategy-performance/. Last on purpose, after the review, and
# deliberately not a step() (same as the log-scan block above): a hang or failure is logged, never
# writes 124 to the status file or manifest, never changes the close-day status, and cannot delay
# value_areas/jobs/track/review. The 120 s cap bounds it. Read-only.
if [ "$(date -d "$DAY" +%u)" -eq 5 ] && [ "$DAY" = "$(date +%F)" ]; then
  if ! timeout --kill-after=10 120 env PYTHONPATH=. "$UV" run python -m services.strategy_performance_report_service \
    --mode both >> "$LOGS/strategy_report_cron.log" 2>&1; then
    echo "WARNING: strategy report failed for $DAY (close-day not affected)" >> "$LOGS/strategy_report_cron.log"
  fi
fi

if [ -f "$REVIEW_REPORT" ]; then
  { echo; echo "## Close-day steps"; echo; echo '```'; column -t -s $'\t' "$STATUS"; echo '```'; } >> "$REVIEW_REPORT"
fi
mf finish
FINISHED=1
echo "=== close day $DAY done $(date +%T); status -> $STATUS ==="
column -t -s $'\t' "$STATUS"
