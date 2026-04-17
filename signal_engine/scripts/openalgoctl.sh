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
LOG_DIR="$PROJECT_DIR/signal_engine/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/openalgoctl.log"
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
    # Any PID alive = running (or partially running)
    if [[ -n "$APP_PID" || -n "$SIGNAL_PID" ]]; then
        return 0
    fi
    # No PIDs but server port is responding
    if curl -fs --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
        return 0
    fi
    return 1
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

# --- Kill processes from PID file ---
#
# NOTE: uv run does NOT forward SIGTERM to its child Python process.
# Killing the uv wrapper PID leaves the actual python3 process running as an orphan.
# We must also kill by matching the project .venv path — specific enough to never
# match unrelated Python processes on this machine.
kill_from_pidfile() {
    local venv_python="$PROJECT_DIR/.venv/bin/python3"

    if [ -f "$PID_FILE" ]; then
        local old_app old_signal
        old_app=$(sed -n '1p' "$PID_FILE")
        old_signal=$(sed -n '2p' "$PID_FILE")

        if [ -n "$old_signal" ] && kill -0 "$old_signal" 2>/dev/null; then
            log "Killing signal_engine uv wrapper (PID $old_signal)..."
            kill "$old_signal" 2>/dev/null || true
        fi

        if [ -n "$old_app" ] && kill -0 "$old_app" 2>/dev/null; then
            log "Killing app.py uv wrapper (PID $old_app)..."
            kill "$old_app" 2>/dev/null || true
        fi
    fi

    # Kill the actual Python processes spawned by uv (uv does not forward SIGTERM).
    # Match by .venv path — safe, project-specific, survives orphaning.
    log "Killing Python processes (venv)..."
    pkill -TERM -f "$venv_python -m signal_engine" 2>/dev/null || true
    pkill -TERM -f "$venv_python app.py" 2>/dev/null || true

    sleep 2

    # Force-kill anything still alive
    if [ -f "$PID_FILE" ]; then
        local old_app old_signal
        old_app=$(sed -n '1p' "$PID_FILE")
        old_signal=$(sed -n '2p' "$PID_FILE")
        [ -n "$old_signal" ] && kill -9 "$old_signal" 2>/dev/null || true
        [ -n "$old_app" ]    && kill -9 "$old_app"    2>/dev/null || true
    fi
    pkill -9 -f "$venv_python -m signal_engine" 2>/dev/null || true
    pkill -9 -f "$venv_python app.py"           2>/dev/null || true

    rm -f "$PID_FILE"
}

# --- Wait for health URL ---
wait_for_health() {
    log "Waiting for server at $HEALTH_URL (max ${MAX_WAIT}s)..."
    local elapsed=0 app_pid="$1"

    while ! curl -fs "$HEALTH_URL" >/dev/null 2>&1; do
        sleep 1
        elapsed=$((elapsed + 1))

        if [ $elapsed -ge $MAX_WAIT ]; then
            log "ERROR: Server did not start within ${MAX_WAIT}s"
            return 1
        fi

        if ! kill -0 "$app_pid" 2>/dev/null; then
            log "ERROR: app.py exited unexpectedly"
            return 1
        fi
    done

    log "Server is ready (took ${elapsed}s)"
}

# --- Core bootstrap: start server + login + signal engine ---
#     Writes PID file as soon as each process starts.
bootstrap() {
    wait_for_network || return 1
    kill_from_pidfile

    # Start OpenAlgo server
    log "Starting OpenAlgo server..."
    "$UV_BIN" run app.py &
    APP_PID=$!

    # Write PID immediately so is_running() detects us
    echo "$APP_PID" > "$PID_FILE"
    echo "" >> "$PID_FILE"

    wait_for_health "$APP_PID" || return 1

    # Startup: auto-login + verify + summary + Telegram notify
    log "Running startup (auto-login, verify, notify)..."

    if "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler startup; then
        log "Startup successful"
    else
        log "ERROR: Startup failed"
        kill "$APP_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        return 1
    fi

    # Start Signal Engine
    log "Starting Signal Engine..."
    "$UV_BIN" run python -m signal_engine.main &
    SIGNAL_PID=$!

    # Update PID file with both PIDs
    echo "$APP_PID" > "$PID_FILE"
    echo "$SIGNAL_PID" >> "$PID_FILE"

    log "All services started:"
    log "  OpenAlgo server  PID=$APP_PID"
    log "  Signal Engine    PID=$SIGNAL_PID"
    log "  PID file: $PID_FILE"
}

# --- Commands ---

cmd_start() {
    # 'start' is an alias for 'run'. The PS1 wrapper uses 'run' directly;
    # this alias exists for interactive use from a WSL terminal.
    cmd_run
}

cmd_run() {
    if is_running; then
        log "RUN SKIPPED: OpenAlgo already running"
        get_pids
        [ -n "$APP_PID" ] && log "  app.py PID: $APP_PID"
        [ -n "$SIGNAL_PID" ] && log "  signal_engine PID: $SIGNAL_PID"
        return
    fi

    log "=========================================="
    log "Starting OpenAlgo (foreground)"
    log "=========================================="
    rotate_log

    exec > >(tee -a "$LOG_FILE") 2>&1

    _STOP_REASON="scheduled"
    _CLEANUP_DONE=false

    # Trap for cleanup in foreground mode
    cleanup() {
        $_CLEANUP_DONE && return
        _CLEANUP_DONE=true
        log "Shutting down (reason: $_STOP_REASON)..."
        timeout 10 "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler shutdown "$_STOP_REASON" 2>&1 || \
            log "Shutdown notification failed (non-fatal)"
        kill "$SIGNAL_PID" 2>/dev/null || true
        kill "$APP_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
        # Note: no 'wait' here — exec > >(tee ...) creates a tee subprocess that
        # won't exit until shell stdout closes, causing 'wait' to deadlock.
        log "Done."
    }
    trap 'cleanup' INT TERM
    trap 'cleanup' EXIT

    bootstrap || exit 1

    log "Blocking — signal engine will auto-restart on crash. Ctrl+C to stop."

    local _MAX_RESTARTS=5
    local _RESTART_COUNT=0
    local _RESTART_WINDOW=300   # seconds: restart budget resets if stable this long
    local _last_start=$SECONDS

    while true; do
        # Wait while both processes are alive
        while kill -0 "$APP_PID" 2>/dev/null && kill -0 "$SIGNAL_PID" 2>/dev/null; do
            sleep 5
            # Reset restart counter if signal engine has been stable for _RESTART_WINDOW seconds
            if (( SECONDS - _last_start >= _RESTART_WINDOW )); then
                _RESTART_COUNT=0
            fi
        done

        # app.py died — fatal, can't recover without it
        if ! kill -0 "$APP_PID" 2>/dev/null; then
            log "app.py exited unexpectedly — cannot recover."
            _STOP_REASON="app_crash"
            break
        fi

        # Signal engine died — attempt restart
        log "Signal engine exited unexpectedly."
        _RESTART_COUNT=$(( _RESTART_COUNT + 1 ))

        if (( _RESTART_COUNT > _MAX_RESTARTS )); then
            log "Signal engine crashed $_RESTART_COUNT times — giving up."
            _STOP_REASON="signal_engine_crash_loop"
            break
        fi

        log "Restarting signal engine (attempt $_RESTART_COUNT/$_MAX_RESTARTS)..."
        sleep 5

        "$UV_BIN" run python -m signal_engine.main &
        SIGNAL_PID=$!
        _last_start=$SECONDS

        # Update PID file
        echo "$APP_PID" > "$PID_FILE"
        echo "$SIGNAL_PID" >> "$PID_FILE"
        log "Signal engine restarted (PID $SIGNAL_PID)"
    done

    cleanup
}

cmd_stop() {
    get_pids

    local venv_python="$PROJECT_DIR/.venv/bin/python3"
    local python_running=false
    pgrep -f "$venv_python app.py" >/dev/null 2>&1 && python_running=true
    pgrep -f "$venv_python -m signal_engine" >/dev/null 2>&1 && python_running=true

    if [[ -z "$APP_PID" && -z "$SIGNAL_PID" && "$python_running" == "false" ]]; then
        log "STOP SKIPPED: OpenAlgo not running"
        rm -f "$PID_FILE"
        return
    fi

    log "STOPPING: Terminating OpenAlgo processes"

    # Send shutdown notification while app.py is still running (Telegram needs it).
    # 10s timeout — if Telegram is slow or the session is busy (signal_engine holds
    # the same Telethon session), this must not block kill_from_pidfile indefinitely.
    log "Sending shutdown notification..."
    timeout 10 "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler shutdown "${STOP_REASON:-scheduled}" 2>&1 || \
        log "Shutdown notification failed (non-fatal)"

    kill_from_pidfile
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
