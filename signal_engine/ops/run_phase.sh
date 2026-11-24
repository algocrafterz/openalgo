#!/usr/bin/env bash
# Run the morning ops phases: open-day (readiness, 09:12) or liveness (09:27). Read-only checks.
#
# Each phase has two steps with separate duties: the check writes its report
# (signal_engine/reports/<phase>-<day>.md and .json); track records failed checks in the bug
# tracker. Nothing is sent to Telegram. Step outcomes go to
# signal_engine/reports/<phase>-<day>-status.tsv for the /logs workflow page and the close-day job
# audit. The close of the day is signal_engine/analysis/close_day.sh.
#
# Run manifest: signal_engine/reports/<phase>-<day>-run.json (state running at the start, a row per
# step, a terminal state at the end: completed, failed, timed_out or interrupted by SIGTERM/SIGINT).
# It is evidence only: a manifest write that fails never stops the phase.
#
# USAGE
#   ./signal_engine/ops/run_phase.sh open-day [YYYY-MM-DD]
#   ./signal_engine/ops/run_phase.sh liveness [YYYY-MM-DD]
#
# CRON (weekdays, IST):
#   12 9 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_phase.sh open-day >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1
#   27 9 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_phase.sh liveness >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

PHASE="${1:?usage: run_phase.sh open-day|liveness [day]}"
DAY="${2:-$(date +%F)}"
case "$PHASE" in open-day|liveness) ;; *) echo "unknown phase $PHASE"; exit 2 ;; esac

if [ "$(date -d "$DAY" +%u)" -gt 5 ]; then
  echo "$DAY is a weekend - no $PHASE."
  exit 0
fi
HOLIDAY="$(sqlite3 "file:$REPO/db/openalgo.db?mode=ro" "SELECT h.description FROM market_holidays h \
  JOIN market_holiday_exchanges e ON e.holiday_id = h.id \
  WHERE h.holiday_date = '$DAY' AND e.exchange_code = 'NSE' LIMIT 1" 2>/dev/null)"
if [ -n "$HOLIDAY" ]; then
  echo "$DAY is an NSE holiday ($HOLIDAY) - no $PHASE."
  exit 0
fi

UV="$(command -v uv || echo /home/anand/.local/bin/uv)"  # cron has a minimal PATH
STATUS="$REPO/signal_engine/reports/$PHASE-$DAY-status.tsv"
mkdir -p "$(dirname "$STATUS")"
printf 'step\tstarted\tended\texit\n' > "$STATUS"

# Run manifest (stdlib python, no uv: about 30 ms). Never fatal.
PY="$(command -v python3 || echo /usr/bin/python3)"
mf() { "$PY" "$REPO/signal_engine/ops/model.py" manifest "$1" --phase "$PHASE" --day "$DAY" "${@:2}" || true; }
FINISHED=0
CHILD=""
on_signal() {  # SIGTERM or SIGINT: stop the running step, record the interruption, leave
  [ -n "$CHILD" ] && kill -TERM "$CHILD" 2>/dev/null
  [ "$FINISHED" -eq 0 ] && mf finish --state interrupted --note "stopped by signal $1"
  FINISHED=1
  exit $(( 128 + $2 ))
}
on_exit() {  # any other way out (an unexpected exit) is also an interruption, never a success
  [ "$FINISHED" -eq 0 ] && mf finish --state interrupted --note "the script ended before its last step"
}
trap 'on_signal TERM 15' TERM
trap 'on_signal INT 2' INT
trap on_exit EXIT
mf start --pid $$

step() {
  local name="$1"; shift
  local started; started="$(date +%T)"
  timeout --kill-after=10 300 "$@" &  # background + wait, so a signal is handled at once
  CHILD=$!
  wait "$CHILD"
  local rc=$?
  CHILD=""
  local ended; ended="$(date +%T)"
  printf '%s\t%s\t%s\t%s\n' "$name" "$started" "$ended" "$rc" >> "$STATUS"
  mf step --name "$name" --started "$started" --ended "$ended" --exit "$rc"
}

echo "=== $PHASE $DAY $(date +%T) ==="
step check env PYTHONPATH=. "$UV" run python -m signal_engine.ops "$PHASE" --day "$DAY"
# Failed checks reach the bug tracker here, in plain code, so a day is recorded even when no AI
# review runs. Safe to repeat: a finding is recorded once per day.
step track env PYTHONPATH=. "$UV" run python -m signal_engine.ops track --day "$DAY"
mf finish
FINISHED=1
column -t -s $'\t' "$STATUS"
