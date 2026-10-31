#!/usr/bin/env bash
# Unattended daily close-day review (the `close-day` skill) - run by close_day.sh after its
# deterministic steps, REPORT-ONLY (never edits code).
#
# WHY THIS EXISTS
# eod.sh's own regression check is deterministic (fixed PASS/FAIL thresholds) - it can flag
# that something failed but cannot judge WHY, whether it's a real bug or known noise (a test
# run's leftover log lines, a routine pre-login auth check, OpenAlgo's own sandbox undercount
# limitation), or what the fix would be. That needs an LLM reading trades.db/the tradebook/the
# logs and reasoning about it, which is exactly what the `/eod` skill does interactively. This
# runs that same investigation unattended, so the user does not have to invoke /eod by hand
# every trading day (2026-09-21).
#
# SAFETY: report-only by construction, not just by instruction. Three independent layers, any
# one of which alone would stop a file edit:
#   1. --permission-mode plan   - Claude Code's structurally read-only mode; nothing executes,
#                                  the final response IS the investigation.
#   2. --disallowedTools Edit,Write,NotebookEdit - the mutating tools are not even registered.
#   3. The prompt itself explicitly forbids edits, commits, and DB mutation.
# --dangerously-skip-permissions is still required for a genuinely unattended run (nobody is
# present to answer an interactive permission prompt) - layers 1-2 are what keep this safe,
# not that flag's absence. If a real bug is found, the report describes the exact fix needed;
# a human applies it later via /eod, same as every fix in signal_engine/docs/PRD.md's 2026-09-21
# entries was applied - with review, tests, and a considered decision on scope each time.
#
# Runs after close_day.sh has taken the tradebook snapshot and written the EOD and execution-health
# reports, so the review reads final books and never re-runs those steps.
#
# USAGE
#   ./signal_engine/analysis/eod_claude_review.sh            # today
#   ./signal_engine/analysis/eod_claude_review.sh 2026-09-08  # a specific session
#
# SCHEDULE: not a cron line of its own any more; close_day.sh (15:36 IST) runs it with a timeout
# that ends before OpenAlgo's 16:00 stop. Hand-run for an earlier day as below.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

DAY="${1:-$(date +%F)}"
REPORTS="$REPO/signal_engine/reports"
mkdir -p "$REPORTS"
OUT="$REPORTS/eod-$DAY-claude-review.md"

# Weekday guard, same reasoning as eod.sh: a hand-run on an NSE holiday should say so plainly
# rather than producing an empty investigation that reads like a bad day.
DOW="$(date -d "$DAY" +%u 2>/dev/null || echo 1)"
if [ "$DOW" -gt 5 ]; then
  echo "$DAY is a weekend - nothing to review."
  exit 0
fi

CLAUDE_BIN="$(command -v claude || echo /home/anand/.local/bin/claude)"
if [ ! -x "$CLAUDE_BIN" ]; then
  echo "FATAL: claude CLI not found"
  exit 1
fi

read -r -d '' PROMPT <<PROMPT_EOF || true
Read .claude/skills/close-day/SKILL.md in this repository and run it for $DAY in its
"Unattended run" mode. close_day.sh has already produced today's deterministic outputs (step
status in signal_engine/reports/close-day-$DAY-status.tsv); read them, do not re-run them.

This is an unattended, REPORT-ONLY run: Edit, Write, and NotebookEdit are unavailable to you by
design, and you must not attempt to work around that (no writing files via shell redirection, no
git commit, no database UPDATE/DELETE/INSERT, no other mutating command of any kind). Describe a
confirmed bug precisely instead - root cause, the evidence you checked, the exact fix - so a
human applies it later. Do not guess at a root cause you have not verified against trades.db,
the broker tradebook, or the logs.

You have a hard time budget (the run is stopped before OpenAlgo shuts down at 16:00): follow
the skill's order, and if time is short finish the verdict, the P&L table and the tracker block
before anything else.

The very last thing in your response is the bug-tracker block described in
.claude/skills/bug-tracker/SKILL.md ("Unattended runs"): a fenced code block with the info
string bug-tracker holding a JSON list (empty list if nothing applies). This script applies it.

Your report is written to $OUT only after you finish, so that file is empty or missing while you
run. That is expected: do not report it as a failure.
PROMPT_EOF

echo "=== EOD Claude review $DAY ==="
"$CLAUDE_BIN" -p "$PROMPT" \
  --permission-mode plan \
  --disallowedTools Edit,Write,NotebookEdit \
  --dangerously-skip-permissions \
  --output-format text \
  > "$OUT.partial" 2>&1
status=$?
mv -f "$OUT.partial" "$OUT"  # whole report or none: a reader never sees it half-written
echo "report -> $OUT (exit $status)"

# The review is read-only; the tracker is written here, through its validating CLI, and only
# with the add/seen/note actions an unattended run may take (signal_engine/bugs/store.py).
UV="$(command -v uv || echo /home/anand/.local/bin/uv)"  # cron has a minimal PATH
APPLIED="$("$UV" run python -m signal_engine.bugs apply "$OUT" --origin "eod-cron-$DAY" 2>&1)"
{
  echo
  echo "## Bug tracker updates (applied by eod_claude_review.sh)"
  echo
  echo "$APPLIED" | sed 's/^/- /'
} >> "$OUT"
exit "$status"
