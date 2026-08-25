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

# --- Log rotation (5MB cap, one compressed generation kept) ---
rotate_log() {
    if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5242880 ]; then
        mv "$LOG_FILE" "$LOG_FILE.old"
        # Compress the rotated copy: it is ~10x smaller and still readable with zless/zgrep.
        rm -f "$LOG_FILE.old.gz"
        gzip -9 "$LOG_FILE.old" 2>/dev/null || true
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

AUTH_COOLDOWN_FILE="$LOG_DIR/auth_cooldown.txt"

# Escalating cooldown after a failed broker auth.
#
# This was a flat 86400s. That is too blunt: on 2026-08-24 a single failure at
# 09:03 blocked 80 subsequent start attempts and cost the entire trading day.
# The point of a cooldown is to avoid hammering the broker's auth API, and a
# few minutes achieves that. Escalate only if failures keep repeating, and cap
# well short of a full session so a morning blip cannot eat the afternoon.
AUTH_COOLDOWN_STEPS=(300 900 3600 10800)  # 5m, 15m, 1h, 3h

# Cooldown state file holds two lines: "<epoch-of-last-failure>" and "<failure-count>".
_cooldown_stamp() { sed -n '1p' "$AUTH_COOLDOWN_FILE" 2>/dev/null || echo 0; }
_cooldown_count() { sed -n '2p' "$AUTH_COOLDOWN_FILE" 2>/dev/null || echo 1; }

# Cooldown length for the Nth consecutive failure (1-based), capped at the last step.
cooldown_secs_for() {
    local n="${1:-1}" last=$(( ${#AUTH_COOLDOWN_STEPS[@]} - 1 ))
    (( n < 1 )) && n=1
    (( n - 1 > last )) && n=$(( last + 1 ))
    echo "${AUTH_COOLDOWN_STEPS[$(( n - 1 ))]}"
}

# Record a failure, escalating the counter, and alert once per failure.
record_auth_failure() {
    local count=1
    if [ -f "$AUTH_COOLDOWN_FILE" ]; then
        count=$(( $(_cooldown_count) + 1 ))
    fi
    local secs; secs=$(cooldown_secs_for "$count")
    {
        date +%s
        echo "$count"
    } > "$AUTH_COOLDOWN_FILE"
    log "Auth failure #${count} — cooling down ${secs}s before the next attempt."
    # Alert out-of-band: a failed startup is otherwise completely silent.
    timeout 20 "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler \
        notify "startup" "Startup failed (attempt ${count}). Retrying after ${secs}s. Signal engine is DOWN." \
        >/dev/null 2>&1 || log "Failure alert could not be sent (non-fatal)"
}

# Check if we're in auth cooldown (too many recent broker auth failures).
# Returns 0 (in cooldown) or 1 (ok to proceed).
check_auth_cooldown() {
    [ -f "$AUTH_COOLDOWN_FILE" ] || return 1
    local stamp elapsed count secs
    stamp=$(_cooldown_stamp)
    count=$(_cooldown_count)
    secs=$(cooldown_secs_for "$count")
    elapsed=$(( $(date +%s) - stamp ))
    if [ "$elapsed" -lt "$secs" ]; then
        local remaining=$(( secs - elapsed ))
        log "AUTH COOLDOWN: auth failure #${count}. Waiting ${remaining}s before retry to avoid API lockout."
        log "To force a retry now: rm $AUTH_COOLDOWN_FILE && openalgoctl.sh run"
        return 0
    fi
    rm -f "$AUTH_COOLDOWN_FILE"
    return 1
}

bootstrap() {
    # Abort early if broker auth failed recently — prevents Task Scheduler from hammering
    # the broker's auth API and triggering account lockout (228+ failures/day pattern).
    if check_auth_cooldown; then
        return 1
    fi

    wait_for_network || return 1
    kill_from_pidfile

    # Wait for NTP sync before starting — required on WSL2 after wake-from-sleep.
    # On wake, the clock can be off by minutes. systemd-timesyncd corrects it within
    # ~10-30s after network is up, but TOTP login fails if attempted before the correction
    # (Flattrade returns "Invalid Input: Invalid OTP" on any clock skew > ~15s).
    local ntp_wait=0
    while [ $ntp_wait -lt 60 ]; do
        if timedatectl show 2>/dev/null | grep -q "NTPSynchronized=yes"; then
            log "NTP synchronized (waited ${ntp_wait}s)"
            break
        fi
        sleep 2
        ntp_wait=$((ntp_wait + 2))
    done
    if [ $ntp_wait -ge 60 ]; then
        log "WARNING: NTP sync timeout after 60s — TOTP login may fail if clock is drifted"
    fi

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
        rm -f "$AUTH_COOLDOWN_FILE"
    else
        log "ERROR: Startup failed — writing auth cooldown to prevent API lockout"
        record_auth_failure
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

    # Readiness probing. kill -0 only proves the PID exists; a wedged app.py
    # holds its PID forever while serving nothing, which the old loop reported
    # as healthy indefinitely. Probe the health URL periodically and require
    # _HEALTH_MAX_FAIL consecutive failures so one slow response is tolerated.
    local _HEALTH_EVERY=60      # seconds between HTTP probes
    local _HEALTH_MAX_FAIL=3    # consecutive failures before declaring it wedged
    local _health_fails=0
    local _last_probe=$SECONDS
    local _app_wedged=false

    while true; do
        # Wait while both processes are alive
        while kill -0 "$APP_PID" 2>/dev/null && kill -0 "$SIGNAL_PID" 2>/dev/null; do
            sleep 5
            # Reset restart counter if signal engine has been stable for _RESTART_WINDOW seconds
            if (( SECONDS - _last_start >= _RESTART_WINDOW )); then
                _RESTART_COUNT=0
            fi

            # Periodic readiness probe
            if (( SECONDS - _last_probe >= _HEALTH_EVERY )); then
                _last_probe=$SECONDS
                if curl -fs --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; then
                    if (( _health_fails > 0 )); then
                        log "Health probe recovered after ${_health_fails} failure(s)."
                    fi
                    _health_fails=0
                else
                    _health_fails=$(( _health_fails + 1 ))
                    log "Health probe FAILED (${_health_fails}/${_HEALTH_MAX_FAIL}) at $HEALTH_URL"
                    if (( _health_fails >= _HEALTH_MAX_FAIL )); then
                        _app_wedged=true
                        break
                    fi
                fi
            fi
        done

        # app.py alive but not serving — the case liveness checks cannot see
        if [ "$_app_wedged" = true ]; then
            log "app.py is alive (PID $APP_PID) but failed ${_health_fails} consecutive health probes — treating as wedged."
            timeout 20 "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler \
                notify "supervisor" "app.py stopped responding on $HEALTH_URL after ${_health_fails} probes. Stack is DOWN." \
                >/dev/null 2>&1 || log "Wedge alert could not be sent (non-fatal)"
            _STOP_REASON="app_unresponsive"
            break
        fi

        # app.py died — fatal, can't recover without it
        if ! kill -0 "$APP_PID" 2>/dev/null; then
            log "app.py exited unexpectedly — cannot recover."
            timeout 20 "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler \
                notify "supervisor" "app.py exited unexpectedly. Stack is DOWN." \
                >/dev/null 2>&1 || log "Crash alert could not be sent (non-fatal)"
            _STOP_REASON="app_crash"
            break
        fi

        # Signal engine died — attempt restart
        log "Signal engine exited unexpectedly."
        _RESTART_COUNT=$(( _RESTART_COUNT + 1 ))

        if (( _RESTART_COUNT > _MAX_RESTARTS )); then
            log "Signal engine crashed $_RESTART_COUNT times — giving up."
            timeout 20 "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler \
                notify "supervisor" "Signal engine crashed ${_RESTART_COUNT} times and will not be restarted. No trades will be taken." \
                >/dev/null 2>&1 || log "Crash-loop alert could not be sent (non-fatal)"
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

cmd_squareoff() {
    log "Squareoff: cancelling orders and closing MIS positions"
    "$UV_BIN" run python -m signal_engine.scripts.openalgoscheduler squareoff 2>&1 | tee -a "$LOG_FILE"
    log "Squareoff: done"
}

# Guard so the file can be sourced by tests without dispatching a command.
if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
    return 0 2>/dev/null || true
fi

case "${1:-}" in
    start)     cmd_start ;;
    run)       cmd_run ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    squareoff) cmd_squareoff ;;
    *)
        echo "Usage: openalgoctl.sh {start|run|stop|restart|status|squareoff}"
        echo ""
        echo "  start      Start in background, return after health check"
        echo "  run        Start in foreground, block until exit (Task Scheduler / systemd)"
        echo "  stop       Stop all services"
        echo "  restart    Stop then start"
        echo "  status     Show running state"
        echo "  squareoff  Cancel orders and close all MIS positions (3:02 PM failsafe)"
        exit 1
esac
