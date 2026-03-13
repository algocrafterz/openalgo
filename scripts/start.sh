#!/usr/bin/env bash
# OpenAlgo + Signal Engine automated startup script.
#
# Prerequisites:
#   1. BROKER_PASSWORD and BROKER_TOTP_SECRET set in .env
#   2. OpenAlgo admin account created (via /setup)
#   3. uv installed
#
# Usage:
#   ./scripts/start.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

HEALTH_URL="http://127.0.0.1:5000/"
MAX_WAIT=60  # seconds to wait for app.py to start (master contract can be slow)

log() { echo "[start.sh] $(date '+%H:%M:%S') $*"; }

# --- 1. Start OpenAlgo server ---
log "Starting OpenAlgo server..."
uv run app.py &
APP_PID=$!

# Wait for server to be ready
log "Waiting for server at $HEALTH_URL (max ${MAX_WAIT}s)..."
elapsed=0
while ! curl -s -o /dev/null -w '' "$HEALTH_URL" 2>/dev/null; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ $elapsed -ge $MAX_WAIT ]; then
        log "ERROR: Server did not start within ${MAX_WAIT}s"
        kill "$APP_PID" 2>/dev/null || true
        exit 1
    fi
    # Check if app.py process is still running
    if ! kill -0 "$APP_PID" 2>/dev/null; then
        log "ERROR: app.py exited unexpectedly"
        exit 1
    fi
done
log "Server is ready (took ${elapsed}s)"

# --- 2. Auto-login to broker ---
log "Running auto-login..."
if uv run python -m scripts.auto_login; then
    log "Auto-login successful"
else
    log "ERROR: Auto-login failed. Check BROKER_PASSWORD and BROKER_TOTP_SECRET in .env"
    kill "$APP_PID" 2>/dev/null || true
    exit 1
fi

# --- 3. Start Signal Engine ---
log "Starting Signal Engine..."
uv run python -m signal_engine.main &
SIGNAL_PID=$!

log "All services started:"
log "  OpenAlgo server  PID=$APP_PID"
log "  Signal Engine    PID=$SIGNAL_PID"
log ""
log "Press Ctrl+C to stop all services."

# Trap Ctrl+C to kill both processes
cleanup() {
    log "Shutting down..."
    kill "$SIGNAL_PID" 2>/dev/null || true
    kill "$APP_PID" 2>/dev/null || true
    wait
    log "Done."
}
trap cleanup INT TERM

# Wait for either process to exit
wait -n "$APP_PID" "$SIGNAL_PID" 2>/dev/null || true
log "A process exited. Shutting down..."
cleanup
