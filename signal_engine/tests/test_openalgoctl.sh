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

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
