#!/usr/bin/env bash
# Run the morning ops phases: open-day (readiness, 09:12) or liveness (09:27). Read-only checks.
#
# Each phase has two steps with separate duties: the check writes its report
# (signal_engine/reports/<phase>-<day>.md and .json); notify sends Telegram only when the verdict
# is RED. Step outcomes go to signal_engine/reports/<phase>-<day>-status.tsv for the /logs
# workflow page and the close-day job audit. The close of the day is signal_engine/analysis/close_day.sh.
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
step() {
  local name="$1"; shift
  local started; started="$(date +%T)"
  timeout --kill-after=10 300 "$@"
  local rc=$?
  printf '%s\t%s\t%s\t%s\n' "$name" "$started" "$(date +%T)" "$rc" >> "$STATUS"
}

echo "=== $PHASE $DAY $(date +%T) ==="
step check env PYTHONPATH=. "$UV" run python -m signal_engine.ops "$PHASE" --day "$DAY"
# Only a same-day run talks to Telegram; a re-run of an earlier day prints the message instead.
NOTIFY_FLAG=""; [ "$DAY" = "$(date +%F)" ] || NOTIFY_FLAG="--dry-run"
step notify env PYTHONPATH=. "$UV" run python -m signal_engine.ops notify "$PHASE" --day "$DAY" $NOTIFY_FLAG
column -t -s $'\t' "$STATUS"
