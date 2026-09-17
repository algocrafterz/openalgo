#!/usr/bin/env bash
# Start/stop/status for the BreakingTrade poller, tracked by PID FILE rather than by
# pattern-matching the process list.
#
# Why this exists: `pkill -f "breakingtrade --watch"` also matches the shell running that very
# command, so it kills itself before starting the replacement - and `pgrep -f` matches its own
# command line too, so it reports the poller as running when nothing is. Both happened on
# 2026-09-04 and cost most of a session's data while every status check said "alive".
#
#   ./poller.sh start    # begin polling (safe to re-run: refuses if already up)
#   ./poller.sh status   # truthful answer
#   ./poller.sh stop
#   ./poller.sh log      # follow output
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
RUN_DIR="$REPO_ROOT/signal_engine/data"
PID_FILE="$RUN_DIR/breakingtrade_poller.pid"
LOG_FILE="$RUN_DIR/breakingtrade_poller.log"

running_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    # Confirm the PID is alive AND is actually our process, not a recycled id.
    if kill -0 "$pid" 2>/dev/null &&
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "signal_engine.analysis.breakingtrade"; then
        echo "$pid"
        return 0
    fi
    return 1
}

case "${1:-status}" in
    start)
        if pid="$(running_pid)"; then
            echo "already running (pid $pid)"
            exit 0
        fi
        mkdir -p "$RUN_DIR"
        cd "$REPO_ROOT"
        # setsid detaches from this shell's process group so the poller survives the session
        # that launched it.
        PYTHONPATH=. PYTHONUNBUFFERED=1 setsid nohup \
            uv run python -m signal_engine.analysis.breakingtrade --watch --volume \
            >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        sleep 2
        if pid="$(running_pid)"; then
            echo "started (pid $pid), logging to $LOG_FILE"
        else
            echo "FAILED to start - see $LOG_FILE" >&2
            exit 1
        fi
        ;;
    stop)
        if pid="$(running_pid)"; then
            kill "$pid" && echo "stopped (pid $pid)"
        else
            echo "not running"
        fi
        rm -f "$PID_FILE"
        ;;
    status)
        if pid="$(running_pid)"; then
            echo "RUNNING (pid $pid)"
            tail -3 "$LOG_FILE" 2>/dev/null || true
        else
            echo "NOT RUNNING"
            exit 1
        fi
        ;;
    log)
        tail -f "$LOG_FILE"
        ;;
    *)
        echo "usage: $0 {start|stop|status|log}" >&2
        exit 2
        ;;
esac
