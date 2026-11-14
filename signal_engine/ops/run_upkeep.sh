#!/usr/bin/env bash
# Unattended upkeep around the daily workflow (signal_engine/ops/automation.py).
#
#   retry-failed   08:56  redo the last session's data download if it failed (once, before 09:10)
#   deadman morning 09:40  alert when the 09:12 or 09:27 checkpoint left no record
#   deadman close   21:35  alert when the close-day run is missing, incomplete or failed. It runs
#                           after the 18:35 and 21:05 review catch-ups so it judges the final state.
#                           (Installed until the maintainer switches the line: 16:20. Both pass the
#                           jobs audit, signal_engine/ops/jobs.py CRON_LINES.)
#
# Weekends and NSE holidays are handled in Python. Nothing here touches orders, positions, config
# or code. Output goes to the ops cron log, which is where the other phases write.
#
# CRON (weekdays, IST):
#   56 8 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_upkeep.sh retry-failed >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1
#   40 9 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_upkeep.sh deadman morning >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1
#   35 21 * * 1-5 /home/anand/github/openalgo/signal_engine/ops/run_upkeep.sh deadman close >> /home/anand/github/openalgo/signal_engine/logs/ops_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

CMD="${1:?usage: run_upkeep.sh retry-failed | deadman morning|close}"
case "$CMD" in
  retry-failed) LIMIT=2700 ;;
  deadman) LIMIT=120 ;;
  *) echo "unknown command $CMD"; exit 2 ;;
esac

UV="$(command -v uv || echo /home/anand/.local/bin/uv)"  # cron has a minimal PATH
echo "=== upkeep $* $(date +%F\ %T) ==="
env PYTHONPATH=. timeout --kill-after=15 "$LIMIT" "$UV" run python -m signal_engine.ops "$@"
echo "upkeep $* exit $?"
