#!/usr/bin/env bash
# Run the full signal_engine test suite, one run at a time per machine.
#
# Overlapping full runs slow each other (10.5 min alone, 15 min and more under load) and make
# tests flaky. This wrapper holds an exclusive lock for the whole run. The lock is released by
# the kernel when this process ends, however it ends, so a crash or a kill cannot leave a stale
# lock behind.
#
# Usage:
#   signal_engine/scripts/run_full_tests.sh [--wait] [--status] [pytest args...]
#
#   (default)  refuse at once, exit 75, if another full run holds the lock
#   --wait     queue behind the running one, then run
#   --status   print who holds the lock, if anyone, and exit
#
# Extra arguments go to pytest, e.g. -x or -k name. The pytest exit code is returned.
# Development feedback should come from targeted test files, not from this script.

set -u

LOCK=/tmp/openalgo-full-tests.lock
INFO=/tmp/openalgo-full-tests.info
WAIT=0
STATUS=0
ARGS=()

for arg in "$@"; do
    case "$arg" in
        --wait) WAIT=1 ;;
        --status) STATUS=1 ;;
        *) ARGS+=("$arg") ;;
    esac
done

holder() {
    if [ -s "$INFO" ]; then
        cat "$INFO"
    else
        echo "holder unknown (no info file)"
    fi
}

exec 9>"$LOCK"

if [ "$STATUS" -eq 1 ]; then
    if flock -n 9; then
        echo "no full-suite run in progress"
        exit 0
    fi
    echo "a full-suite run is in progress:"
    holder
    exit 75
fi

if [ "$WAIT" -eq 1 ]; then
    if ! flock -n 9; then
        echo "a full-suite run is in progress; waiting for it:"
        holder
        flock 9
    fi
else
    if ! flock -n 9; then
        echo "REFUSED: a full-suite run is already in progress." >&2
        holder >&2
        echo "Use targeted test files for development feedback, or re-run with --wait to queue." >&2
        exit 75
    fi
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

{
    echo "pid=$$"
    echo "started=$(date '+%Y-%m-%d %H:%M:%S')"
    echo "host=$(hostname)"
    echo "args=${ARGS[*]:-}"
} > "$INFO"
trap 'rm -f "$INFO"' EXIT

# Unbuffered, so a log file shows real progress; durations show where the time goes.
PYTHONUNBUFFERED=1 PYTHONPATH=. uv run pytest signal_engine/tests --durations=25 -q \
    -p no:cacheprovider "${ARGS[@]}"
exit $?
