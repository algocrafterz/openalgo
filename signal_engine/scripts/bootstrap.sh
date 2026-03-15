#!/usr/bin/env bash
# OpenAlgo + Signal Engine automated startup script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# signal_engine/scripts/ -> project root (two levels up)
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR"

# --- Ensure uv is available when run via automation ---
export PATH="$HOME/.local/bin:$PATH"

UV_BIN="$(command -v uv || true)"

if [ -z "$UV_BIN" ]; then
    echo "ERROR: uv not found in PATH"
    echo "Install uv with: pip install uv"
    exit 1
fi

# --- Logging setup ---
LOG_DIR="$PROJECT_DIR/signal_engine/log"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/startup.log"
PID_FILE="$PROJECT_DIR/signal_engine/openalgo.pid"

# Rotate log if > 5MB
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    mv "$LOG_FILE" "$LOG_FILE.old"
fi

exec > >(tee -a "$LOG_FILE") 2>&1

HEALTH_URL="http://127.0.0.1:5000/"
MAX_WAIT=60
NET_MAX_WAIT=120

log() { echo "[bootstrap] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

log "=========================================="
log "Starting OpenAlgo (PID: $$)"
log "=========================================="

# --- Wait for network (important after wake-from-sleep) ---
log "Checking network connectivity..."

net_elapsed=0

while ! curl -fs --max-time 3 https://httpbin.org/status/200 >/dev/null 2>&1; do
    sleep 2
    net_elapsed=$((net_elapsed + 2))

    if [ $net_elapsed -ge $NET_MAX_WAIT ]; then
        log "ERROR: No network after ${NET_MAX_WAIT}s"
        exit 1
    fi

    log "Waiting for network... (${net_elapsed}s)"
done

log "Network is up (took ${net_elapsed}s)"

# --- Kill stale processes using PID file ---
if [ -f "$PID_FILE" ]; then
    OLD_APP_PID=$(sed -n '1p' "$PID_FILE")
    OLD_SIGNAL_PID=$(sed -n '2p' "$PID_FILE")

    if [ -n "$OLD_APP_PID" ] && kill -0 "$OLD_APP_PID" 2>/dev/null; then
        log "Killing stale app.py (PID $OLD_APP_PID)..."
        kill "$OLD_APP_PID" 2>/dev/null || true
        sleep 2
    fi

    if [ -n "$OLD_SIGNAL_PID" ] && kill -0 "$OLD_SIGNAL_PID" 2>/dev/null; then
        log "Killing stale signal_engine (PID $OLD_SIGNAL_PID)..."
        kill "$OLD_SIGNAL_PID" 2>/dev/null || true
        sleep 2
    fi

    rm -f "$PID_FILE"
fi

# --- Trap for cleanup ---
cleanup() {
    log "Shutting down..."

    kill "$SIGNAL_PID" 2>/dev/null || true
    kill "$APP_PID" 2>/dev/null || true

    rm -f "$PID_FILE"
    wait || true

    log "Done."
}

trap cleanup INT TERM EXIT

# --- Start OpenAlgo server ---
log "Starting OpenAlgo server..."

"$UV_BIN" run app.py &
APP_PID=$!

log "Waiting for server at $HEALTH_URL (max ${MAX_WAIT}s)..."

elapsed=0

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

# --- Startup (login + verify + notify) ---
log "Running startup (auto-login, verify, notify)..."

if "$UV_BIN" run python -m signal_engine.scripts.startup; then
    log "Startup successful"
else
    log "ERROR: Auto-login failed"
    kill "$APP_PID" 2>/dev/null || true
    exit 1
fi

# --- Start Signal Engine ---
log "Starting Signal Engine..."

"$UV_BIN" run python -m signal_engine.main &
SIGNAL_PID=$!

# --- Write PID file ---
echo "$APP_PID" > "$PID_FILE"
echo "$SIGNAL_PID" >> "$PID_FILE"

log "All services started:"
log "  OpenAlgo server  PID=$APP_PID"
log "  Signal Engine    PID=$SIGNAL_PID"
log "  PID file: $PID_FILE"
log ""
log "Press Ctrl+C to stop all services."

# Wait until one process exits
wait -n "$APP_PID" "$SIGNAL_PID" || true

log "A process exited. Shutting down..."

cleanup
