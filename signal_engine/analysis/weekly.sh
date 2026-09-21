#!/usr/bin/env bash
# Weekly rollup for the signal engine. Cron-safe, idempotent.
#
# Runs after the last trading day's eod.sh has already produced its daily report, and
# writes one dated markdown report:
#   signal_engine/analysis/reports/weekly-YYYY-MM-DD-to-YYYY-MM-DD.md
#
# WHY A SCRIPT RATHER THAN RE-READING THE FIVE DAILY REPORTS BY HAND
# The daily eod-*.md reports already hold everything this needs - this just does the
# arithmetic across the week once, instead of a human (or an LLM) re-reading five files
# and adding it up, every week, forever.
#
# USAGE
#   ./signal_engine/analysis/weekly.sh                          # last completed Mon-Fri week
#   ./signal_engine/analysis/weekly.sh 2026-09-14 2026-09-18    # a specific week
#
# CRON (Saturday, well after the week's last eod.sh run):
#   0 9 * * 6 /home/anand/github/openalgo/signal_engine/analysis/weekly.sh >> /home/anand/github/openalgo/signal_engine/logs/weekly_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

LOGS="$REPO/signal_engine/logs"
LOCK="$LOGS/.weekly.lock"
mkdir -p "$REPO/signal_engine/analysis/reports" "$LOGS"

# Overlap guard: a hand-run and the cron run must not write the same file at once.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "FATAL: another weekly run is in progress ($LOCK)"
  exit 1
fi

UV="$(command -v uv || echo /usr/local/bin/uv)"

if [ "$#" -eq 2 ]; then
  echo "=== WEEKLY $1 to $2 ==="
  PYTHONPATH="$REPO" "$UV" run python -m signal_engine.analysis.weekly_review --since "$1" --until "$2"
else
  echo "=== WEEKLY (last completed Mon-Fri week) ==="
  PYTHONPATH="$REPO" "$UV" run python -m signal_engine.analysis.weekly_review
fi
