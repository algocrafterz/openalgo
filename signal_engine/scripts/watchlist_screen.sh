#!/usr/bin/env bash
# Manual/testing wrapper for the ORB/BREAKOUT/EMA9VWAP watchlist screen. NOT on a
# cron schedule - see signal_engine/scripts/watchlist_screen.py's module docstring.
#
# In normal operation this runs itself: openalgoscheduler.py's startup flow calls
# maybe_run_monthly_screen() as its last step, on whatever day the trading bot actually
# next starts up (gated so it only does real work once per calendar month). That avoids
# depending on the machine being on at some fixed clock time, and keeps the Telegram
# send sequential with (never racing) the live listener's own Telethon session.
#
# Use this script by hand to check the screen mid-month, or to re-run after fixing a
# failure, without waiting for the next startup:
#
# USAGE
#   ./signal_engine/scripts/watchlist_screen.sh             # run if not already run this month
#   ./signal_engine/scripts/watchlist_screen.sh --dry-run   # print only, no file/send/gate
#   ./signal_engine/scripts/watchlist_screen.sh --force     # run even if already run this month

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

LOGS="$REPO/signal_engine/logs"
LOCK="$LOGS/.watchlist_screen.lock"
mkdir -p "$LOGS"

# Overlap guard: a hand-run and the cron run must not refresh/write at the same time.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "FATAL: another watchlist screen run is in progress ($LOCK)"
  exit 1
fi

UV="$(command -v uv || echo /usr/local/bin/uv)"

echo "=== watchlist screen, $(date '+%Y-%m-%d %H:%M %Z') ==="
PYTHONPATH="$REPO" "$UV" run --group analysis python -m signal_engine.scripts.watchlist_screen "$@"
STATUS=$?
echo "exit status: $STATUS"
exit "$STATUS"
