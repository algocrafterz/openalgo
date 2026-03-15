#!/usr/bin/env bash
# OpenAlgo service controller — start/stop/restart/status.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

START_SCRIPT="$SCRIPT_DIR/bootstrap.sh"
PID_FILE="$PROJECT_DIR/signal_engine/openalgo.pid"
LOG_DIR="$PROJECT_DIR/signal_engine/log"
LOGFILE="$LOG_DIR/openalgoctl.log"
HEALTH_URL="http://127.0.0.1:5000/"
MAX_WAIT=90

mkdir -p "$LOG_DIR"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOGFILE"
}

get_pids() {
    # Returns app_pid and signal_pid from PID file, validated against running processes
    APP_PID=""
    SIGNAL_PID=""

    if [ -f "$PID_FILE" ]; then
        APP_PID=$(sed -n '1p' "$PID_FILE")
        SIGNAL_PID=$(sed -n '2p' "$PID_FILE")

        # Validate PIDs are still running
        if [ -n "$APP_PID" ] && ! kill -0 "$APP_PID" 2>/dev/null; then
            APP_PID=""
        fi
        if [ -n "$SIGNAL_PID" ] && ! kill -0 "$SIGNAL_PID" 2>/dev/null; then
            SIGNAL_PID=""
        fi
    fi
}

status() {
    get_pids

    if [[ -n "$APP_PID" && -n "$SIGNAL_PID" ]]; then
        log "STATUS: OpenAlgo running"
        log "  app.py PID: $APP_PID"
        log "  signal_engine PID: $SIGNAL_PID"
    elif [[ -n "$APP_PID" ]]; then
        log "STATUS: Partial — app.py running (PID $APP_PID), signal_engine not running"
    elif [[ -n "$SIGNAL_PID" ]]; then
        log "STATUS: Partial — signal_engine running (PID $SIGNAL_PID), app.py not running"
    else
        log "STATUS: OpenAlgo not running"
    fi
}

start() {
    get_pids

    if [[ -n "$APP_PID" && -n "$SIGNAL_PID" ]]; then
        log "START SKIPPED: OpenAlgo already running"
        log "  app.py PID: $APP_PID"
        log "  signal_engine PID: $SIGNAL_PID"
        return
    fi

    log "STARTING: Launching OpenAlgo..."

    nohup "$START_SCRIPT" >> "$LOG_DIR/startup.log" 2>&1 &

    # Poll health endpoint instead of blind sleep
    log "WAITING: Polling $HEALTH_URL (max ${MAX_WAIT}s)..."
    elapsed=0

    while ! curl -fs "$HEALTH_URL" >/dev/null 2>&1; do
        sleep 2
        elapsed=$((elapsed + 2))

        if [ $elapsed -ge $MAX_WAIT ]; then
            log "START FAILURE: Server not ready after ${MAX_WAIT}s"
            tail -n 20 "$LOG_DIR/startup.log" 2>/dev/null || true
            exit 1
        fi
    done

    # Re-read PIDs after startup
    get_pids

    if [[ -n "$APP_PID" && -n "$SIGNAL_PID" ]]; then
        log "START SUCCESS: OpenAlgo running"
        log "  app.py PID: $APP_PID"
        log "  signal_engine PID: $SIGNAL_PID"
    elif [[ -n "$APP_PID" ]]; then
        log "START PARTIAL: app.py running, signal_engine may still be starting"
    else
        log "START FAILURE: Services did not start correctly"
        tail -n 20 "$LOG_DIR/startup.log" 2>/dev/null || true
        exit 1
    fi
}

stop() {
    get_pids

    if [[ -z "$APP_PID" && -z "$SIGNAL_PID" ]]; then
        log "STOP SKIPPED: OpenAlgo not running"
        rm -f "$PID_FILE"
        return
    fi

    log "STOPPING: Terminating OpenAlgo processes"

    [ -n "$SIGNAL_PID" ] && kill "$SIGNAL_PID" 2>/dev/null || true
    [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true

    sleep 3

    # Force-kill if still running
    get_pids

    if [[ -n "$APP_PID" || -n "$SIGNAL_PID" ]]; then
        log "STOP WARNING: Some processes still running, forcing termination"
        [ -n "$APP_PID" ] && kill -9 "$APP_PID" 2>/dev/null || true
        [ -n "$SIGNAL_PID" ] && kill -9 "$SIGNAL_PID" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    log "STOP SUCCESS: OpenAlgo stopped"
}

restart() {
    log "RESTART REQUESTED"
    stop
    sleep 3
    start
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    status) status ;;
    *)
        echo "Usage: openalgoctl.sh {start|stop|restart|status}"
        exit 1
esac
