#!/usr/bin/env bash
# Tests for the availability/cooldown logic in scripts/openalgoctl.sh.
#
# Regression guard for 2026-08-24: one broker-auth failure at 09:03 wrote a flat
# 24h cooldown, which then blocked 80 start attempts for the rest of the trading
# day -- silently, because the startup path exited before notifying anything.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTL="$SCRIPT_DIR/../scripts/openalgoctl.sh"

PASS=0
FAIL=0
check() {
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1)); echo "  ok   $1"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL $1 (expected '$3', got '$2')"
    fi
}

# shellcheck source=/dev/null
source "$CTL"

# openalgoctl.sh sets `set -euo pipefail`; under -e a helper returning non-zero
# (which is exactly what these tests assert on) would abort the run.
set +e

# Isolate state and neutralise the outbound alert.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
AUTH_COOLDOWN_FILE="$TMP/auth_cooldown.txt"
UV_BIN="/bin/true"
log() { :; }   # silence during tests

echo "cooldown_secs_for: escalation and cap"
check "1st failure = 5m"      "$(cooldown_secs_for 1)" "300"
check "2nd failure = 15m"     "$(cooldown_secs_for 2)" "900"
check "3rd failure = 1h"      "$(cooldown_secs_for 3)" "3600"
check "4th failure = 3h"      "$(cooldown_secs_for 4)" "10800"
check "caps at 3h"            "$(cooldown_secs_for 99)" "10800"
check "clamps below 1"        "$(cooldown_secs_for 0)" "300"

echo "check_auth_cooldown: state transitions"
rm -f "$AUTH_COOLDOWN_FILE"
check_auth_cooldown; check "no state file => not in cooldown" "$?" "1"

record_auth_failure
check "failure count recorded" "$(sed -n '2p' "$AUTH_COOLDOWN_FILE")" "1"
check_auth_cooldown; check "fresh failure => in cooldown" "$?" "0"

record_auth_failure
check "second failure escalates count" "$(sed -n '2p' "$AUTH_COOLDOWN_FILE")" "2"

# Backdate past the 15m window for failure #2 -> cooldown must lift and self-clear.
{ echo "$(( $(date +%s) - 1000 ))"; echo "2"; } > "$AUTH_COOLDOWN_FILE"
check_auth_cooldown; check "expired cooldown => allowed through" "$?" "1"
check "expired cooldown self-clears" "$([ -f "$AUTH_COOLDOWN_FILE" ] && echo present || echo gone)" "gone"

# A blip must not cost the trading day: worst case before retry is well under a session.
check "max cooldown < NSE session (6.25h)" "$(( $(cooldown_secs_for 99) < 22500 ))" "1"

echo "acquire_lock: single-instance guard (regression guard for the 2026-09-15"
echo "kill/relaunch churn, and for cmd_restart racing the old run's teardown)"

# LOCK_WAIT_SECS overrides the real 20s so these run in a few seconds, not 20+.
_try_acquire() {
    # Subshell: acquire_lock() calls `exit 2` on refusal, which must not kill
    # the test runner. "acquired" only prints if acquire_lock() returned.
    ( LOCK_FILE="$1" LOCK_WAIT_SECS="$2" UV_BIN="/bin/true" acquire_lock \
        && echo acquired ) 2>/dev/null
}

check "free lock is acquired immediately" \
    "$(_try_acquire "$TMP/lock_free" 1)" "acquired"

# Held briefly by another process, released well inside the wait window.
lock_brief="$TMP/lock_brief"
( exec 8>"$lock_brief"; flock 8; sleep 1 ) &
holder=$!
sleep 0.2  # let the holder actually acquire before we contend for it
check "waits out a brief hold and then acquires" \
    "$(_try_acquire "$lock_brief" 3)" "acquired"
wait "$holder" 2>/dev/null

# Still held after the wait window elapses -> refused, not an infinite hang.
lock_long="$TMP/lock_long"
( exec 8>"$lock_long"; flock 8; sleep 3 ) &
holder=$!
sleep 0.2
check "gives up and refuses when still held after the wait" \
    "$(_try_acquire "$lock_long" 1)" ""
wait "$holder" 2>/dev/null

# --- kill_from_pidfile: signal_engine gets time to finish before the -9 -------------------------
# Regression guard for finding I-05: TERM, a fixed 2 s sleep, then `pkill -9`, which cut the engine
# off in the middle of its own shutdown (an order in flight is then at the broker with nothing
# tracking it). Every process command is replaced by a function inside a subshell, so this test
# never signals a real process: `kill`, `pkill`, `pgrep` and `sleep` below are stubs.
echo "kill_from_pidfile: stop grace for signal_engine"

_stop_run() {
    # $1 = number of pgrep polls during which signal_engine still reports as running
    (
        PID_FILE="$TMP/no_such.pid"
        PROJECT_DIR="$TMP/project"
        ACTIONS="$TMP/actions.$RANDOM"
        : > "$ACTIONS"
        POLLS=0
        STILL_RUNNING_FOR="$1"
        kill() { echo "kill $*" >> "$ACTIONS"; return 0; }
        # A `pkill -9` on the engine only matters when it is still running at that moment.
        pkill() {
            # Record every TERM with whether the engine was still reported running at that moment.
            if [ "$1" = "-TERM" ]; then
                local who=app; [[ "$*" == *signal_engine.main* ]] && who=engine
                local alive=no; [ "$POLLS" -lt "$STILL_RUNNING_FOR" ] && alive=yes
                echo "term_$who engine_alive=$alive" >> "$ACTIONS"
            fi
            if [ "$1" = "-9" ] && [[ "$*" == *signal_engine.main* ]] \
                && [ "$POLLS" -lt "$STILL_RUNNING_FOR" ]; then
                echo "killed_live" >> "$ACTIONS"
            fi
            return 0
        }
        pgrep() {
            POLLS=$((POLLS + 1))
            [ "$POLLS" -le "$STILL_RUNNING_FOR" ]
        }
        sleep() { echo "sleep $*" >> "$ACTIONS"; }
        kill_from_pidfile
        echo "sleeps=$(grep -c '^sleep' "$ACTIONS") hard_kills=$(grep -c '^killed_live' "$ACTIONS")"
        grep '^term_' "$ACTIONS" > "$TMP/terms.last"
    )
}

# Exits after ~3 s: waited for it, no force-kill of the engine.
out="$(_stop_run 6)"
check "engine that exits on TERM is not force-killed"        "${out##*hard_kills=}" "0"
check "waits in 0.5 s steps while the engine is running"    "$([ "${out%% hard_kills*}" != "sleeps=0" ] && echo yes)" "yes"

# Never exits: the full 30 s (60 half-second polls) pass before the -9.
out="$(_stop_run 100000)"
# 60 polls for the engine, then the 4 polls (2 s) app.py is given after its own TERM.
check "engine that ignores TERM waits the full 30 s"       "${out%% hard_kills*}" "sleeps=64"
check "engine that ignores TERM is force-killed after it"  "${out##*hard_kills=}" "1"

# Already gone at the first poll: still waits the old 2 s floor for app.py, then continues.
out="$(_stop_run 0)"
check "keeps the old 2 s wait when the engine is already gone" "${out%% hard_kills*}" "sleeps=4"

# Stop order: the engine's shutdown drain finishes its in-flight orders through OpenAlgo, so
# app.py gets TERM only after the engine has exited.
_stop_run 6 > /dev/null
check "engine is sent TERM first, app.py second" \
    "$(sed 's/ engine_alive=.*//' "$TMP/terms.last" | tr '\n' ' ')" "term_engine term_app "
check "app.py is not sent TERM while the engine still runs" \
    "$(grep '^term_app' "$TMP/terms.last")" "term_app engine_alive=no"
_stop_run 0 > /dev/null
check "app.py is sent TERM at once when the engine is already gone" \
    "$(sed 's/ engine_alive=.*//' "$TMP/terms.last" | tr '\n' ' ')" "term_engine term_app "

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
