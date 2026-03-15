#!/usr/bin/env bash
# OpenAlgo + Signal Engine — unified service controller.
#
# Usage:
#   openalgoctl.sh start    — start in background, return after health check
#   openalgoctl.sh run      — start in foreground, block until exit (for Task Scheduler / systemd)
#   openalgoctl.sh stop     — stop all services
#   openalgoctl.sh restart  — stop then start
#   openalgoctl.sh status   — show running state

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

# --- Ensure uv is available when run via automation ---
export PATH="$HOME/.local/bin:$PATH"

UV_BIN="$(command -v uv || true)"

if [ -z "$UV_BIN" ]; then
    echo "ERROR: uv not found in PATH. Install with: pip install uv"
    exit 1
fi

# --- Paths ---
LOG_DIR="$PROJECT_DIR/signal_engine/log"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/startup.log"
PID_FILE="$PROJECT_DIR/signal_engine/openalgo.pid"
HEALTH_URL="http://127.0.0.1:5000/"
MAX_WAIT=90
NET_MAX_WAIT=120

log() { echo "[openalgoctl] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# --- Log rotation (5MB cap) ---
rotate_log() {
    if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5242880 ]; then
        mv "$LOG_FILE" "$LOG_FILE.old"
    fi
}

# --- PID file helpers ---
get_pids() {
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

is_running() {
    get_pids
    [[ -n "$APP_PID" && -n "$SIGNAL_PID" ]]
}

# --- Wait for network ---
wait_for_network() {
    log "Checking network connectivity..."
    local elapsed=0

    while ! curl -fs --max-time 3 https://httpbin.org/status/200 >/dev/null 2>&1; do
        sleep 2
        elapsed=$((elapsed + 2))

        if [ $elapsed -ge $NET_MAX_WAIT ]; then
            log "ERROR: No network after ${NET_MAX_WAIT}s"
            return 1
        fi

        log "Waiting for network... (${elapsed}s)"
    done

    log "Network is up (took ${elapsed}s)"
}

# --- Kill stale processes from PID file ---
kill_stale() {
    if [ -f "$PID_FILE" ]; then
        local old_app old_signal
        old_app=$(sed -n '1p' "$PID_FILE")
        old_signal=$(sed -n '2p' "$PID_FILE")

        if [ -n "$old_app" ] && kill -0 "$old_app" 2>/dev/null; then
            log "Killing stale app.py (PID $old_app)..."
            kill "$old_app" 2>/dev/null || true
            sleep 2
        fi

        if [ -n "$old_signal" ] && kill -0 "$old_signal" 2>/dev/null; then
            log "Killing stale signal_engine (PID $old_signal)..."
            kill "$old_signal" 2>/dev/null || true
            sleep 2
        fi

        rm -f "$PID_FILE"
    fi
}

# --- Core bootstrap: start server + login + signal engine ---
bootstrap() {
    wait_for_network || exit 1
    kill_stale

    # Start OpenAlgo server
    log "Starting OpenAlgo server..."
    "$UV_BIN" run app.py &
    APP_PID=$!

    log "Waiting for server at $HEALTH_URL (max ${MAX_WAIT}s)..."
    local elapsed=0

    while ! curl -fs "$HEALTH_URL" >/dev/null 2>&1; do
        sleep 1
        elapsed=$((elapsed + 1))

        if [ $elapsed -ge $MAX_WAIT ]; then
            log "ERROR: Server did not start within ${MAX_WAIT}s"
            kill "$APP_PID" 2>/dev/null || true
            exit 1
        fi

        if ! kill -0 "$APP_PID" 2>/dev/null; then
            log "ERROR: app.py exited unexpectedly"
            exit 1
        fi
    done

    log "Server is ready (took ${elapsed}s)"

    # Startup: auto-login + verify + summary + Telegram notify
    log "Running startup (auto-login, verify, notify)..."

    if "$UV_BIN" run python -m signal_engine.scripts.openalgostartup; then
        log "Startup successful"
    else
        log "ERROR: Startup failed"
        kill "$APP_PID" 2>/dev/null || true
        exit 1
    fi

    # Start Signal Engine
    log "Starting Signal Engine..."
    "$UV_BIN" run python -m signal_engine.main &
    SIGNAL_PID=$!

    # Write PID file
    echo "$APP_PID" > "$PID_FILE"
    echo "$SIGNAL_PID" >> "$PID_FILE"

    log "All services started:"
    log "  OpenAlgo server  PID=$APP_PID"
    log "  Signal Engine    PID=$SIGNAL_PID"
    log "  PID file: $PID_FILE"
}

# --- Commands ---

cmd_start() {
    if is_running; then
        log "START SKIPPED: OpenAlgo already running"
        log "  app.py PID: $APP_PID"
        log "  signal_engine PID: $SIGNAL_PID"
        return
    fi

    log "STARTING: Launching OpenAlgo (background)..."
    rotate_log

    # Run bootstrap in a subshell, detached
    (
        exec > >(tee -a "$LOG_FILE") 2>&1
        bootstrap
        # Keep waiting so services stay alive
        wait -n "$APP_PID" "$SIGNAL_PID" || true
        log "A process exited. Shutting down..."
        kill "$SIGNAL_PID" 2>/dev/null || true
        kill "$APP_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        wait || true
        log "Done."
    ) &
    disown

    # Poll health URL until services are up
    log "WAITING: Polling $HEALTH_URL (max ${MAX_WAIT}s)..."
    local elapsed=0

    while ! curl -fs "$HEALTH_URL" >/dev/null 2>&1; do
        sleep 2
        elapsed=$((elapsed + 2))

        if [ $elapsed -ge $MAX_WAIT ]; then
            log "START FAILURE: Server not ready after ${MAX_WAIT}s"
            tail -n 20 "$LOG_FILE" 2>/dev/null || true
            exit 1
        fi
    done

    # Give signal engine a moment to start and write PID file
    sleep 5
    get_pids

    if [[ -n "$APP_PID" && -n "$SIGNAL_PID" ]]; then
        log "START SUCCESS: OpenAlgo running"
        log "  app.py PID: $APP_PID"
        log "  signal_engine PID: $SIGNAL_PID"
    elif [[ -n "$APP_PID" ]]; then
        log "START PARTIAL: app.py running, signal_engine may still be starting"
    else
        log "START FAILURE: Services did not start correctly"
        tail -n 20 "$LOG_FILE" 2>/dev/null || true
        exit 1
    fi
}

cmd_run() {
    if is_running; then
        log "RUN SKIPPED: OpenAlgo already running"
        log "  app.py PID: $APP_PID"
        log "  signal_engine PID: $SIGNAL_PID"
        return
    fi

    log "=========================================="
    log "Starting OpenAlgo (foreground)"
    log "=========================================="
    rotate_log

    exec > >(tee -a "$LOG_FILE") 2>&1

    # Trap for cleanup in foreground mode
    cleanup() {
        log "Shutting down..."
        kill "$SIGNAL_PID" 2>/dev/null || true
        kill "$APP_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        wait || true
        log "Done."
    }
    trap cleanup INT TERM EXIT

    bootstrap

    log "Press Ctrl+C to stop all services."

    # Block until one process exits
    wait -n "$APP_PID" "$SIGNAL_PID" || true
    log "A process exited. Shutting down..."
    cleanup
}

cmd_stop() {
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

cmd_restart() {
    log "RESTART REQUESTED"
    cmd_stop
    sleep 3
    cmd_start
}

cmd_status() {
    get_pids

    if [[ -n "$APP_PID" && -n "$SIGNAL_PID" ]]; then
        log "STATUS: OpenAlgo running"
        log "  app.py PID: $APP_PID"
        log "  signal_engine PID: $SIGNAL_PID"
    elif [[ -n "$APP_PID" ]]; then
        log "STATUS: Partial -- app.py running (PID $APP_PID), signal_engine not running"
    elif [[ -n "$SIGNAL_PID" ]]; then
        log "STATUS: Partial -- signal_engine running (PID $SIGNAL_PID), app.py not running"
    else
        log "STATUS: OpenAlgo not running"
    fi
}

case "${1:-}" in
    start)   cmd_start ;;
    run)     cmd_run ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    *)
        echo "Usage: openalgoctl.sh {start|run|stop|restart|status}"
        echo ""
        echo "  start    Start in background, return after health check"
        echo "  run      Start in foreground, block until exit (Task Scheduler / systemd)"
        echo "  stop     Stop all services"
        echo "  restart  Stop then start"
        echo "  status   Show running state"
        exit 1
esac
