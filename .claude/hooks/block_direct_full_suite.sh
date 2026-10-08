#!/usr/bin/env bash
# PreToolUse hook for the Bash tool: one full signal_engine test-suite run at a time.
#
# A direct `pytest signal_engine/tests` (the whole directory) is denied. Sessions run the full
# suite through signal_engine/scripts/run_full_tests.sh, which holds a machine-wide lock.
# Targeted runs (a test file or node id) and --collect-only are allowed: they are the
# development tier and cost seconds. See "Run tests in two tiers" in CLAUDE.md.

cmd="$(jq -r '.tool_input.command // ""' 2>/dev/null)"

# The whole directory: `signal_engine/tests` followed by a space, a quote, a slash then a
# space, or the end of the command, and not by a file name.
full_suite='pytest[^|;&]*signal_engine/tests/?([[:space:]"'"'"']|$)'

if printf '%s' "$cmd" | grep -Eq "$full_suite" \
    && ! printf '%s' "$cmd" | grep -Eq -- '--collect-only|--co([[:space:]]|$)'; then
    reason="Blocked: run the full signal_engine suite through signal_engine/scripts/run_full_tests.sh, which allows one run at a time across sessions (add --wait to queue behind a running one, --status to see who holds the lock). For development feedback run only the affected test files."
    jq -n --arg reason "$reason" '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: $reason
        }
    }'
fi
exit 0
