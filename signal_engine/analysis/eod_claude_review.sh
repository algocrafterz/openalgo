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
# An unattended job outlives an interactive sign-in. If the owner saved a long-lived token from
# `claude setup-token` in this file (mode 600, outside the repo), use it. It is never printed.
TOKEN_FILE="${CLAUDE_TOKEN_FILE:-$HOME/.config/openalgo/claude_token}"
if [ -r "$TOKEN_FILE" ]; then CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"; export CLAUDE_CODE_OAUTH_TOKEN; fi

if [ ! -x "$CLAUDE_BIN" ]; then
  echo "FATAL: claude CLI not found"
  exit 1
fi

# Concrete clock times for the prompt. A same-day run ends at close_day.sh's 15:58 deadline and
# the review stops investigating at 15:55; a re-run of an earlier day gets the same 20 minutes
# close_day.sh gives it. A model told only "hard time budget" stopped after about a minute with
# thirteen unused (2026-11-02, 2026-11-03).
if [ "$DAY" = "$(date +%F)" ] && [ "$(date +%s)" -lt "$(date -d "$DAY 15:50" +%s)" ]; then
  HARD_END="$(date -d "$DAY 15:58" +%s)"
  STOP_AT="$(date -d "$DAY 15:55" +%H:%M)"
else
  HARD_END="$(( $(date +%s) + ${CLOSE_DAY_BUDGET_SECONDS:-1200} ))"
  STOP_AT="$(date -d "@$(( HARD_END - 180 ))" +%H:%M)"
fi
HARD_AT="$(date -d "@$HARD_END" +%H:%M)"

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

Before anything else, in this order:
1. Run: uv run python -m signal_engine.bugs list --all
2. Read the "Unattended runs" section of .claude/skills/bug-tracker/SKILL.md in full. It holds the
   rules for the tracker block at the end of your response.
3. Run: date

Every section of the skill's report is required, including section 4 (week to date and since
start) and section 9 (tomorrow). Never skip a section silently. If its evidence is not available,
or you ran out of time, keep the section heading and write what is missing and why.

The tracker block must hold one note action for every bug whose status is fixed in the list from
step 1. Each note says whether $DAY exercised that bug's fixed path and what you found, with the
evidence you checked (a log line, a trades.db row, a report line). "Not exercised" is a valid note
when the evidence shows the trigger did not occur; say what you looked for. A problem you could
not confirm goes in the report as unconfirmed, not in the tracker block.

Timing: the run is stopped at $HARD_AT, and a stopped run applies no tracker changes. Stop
investigating at $STOP_AT (check with date). By then write the sections still missing from what
you have, then the verdict block, then the tracker block.

Just before that last block, write the day's verdict as a fenced code block with the info string
close-day-verdict holding one JSON object: {"verdict": "GREEN" | "AMBER" | "RED", "line": "<one
sentence a trader reads on Telegram: net P&L after charges and the one thing that needs attention,
or 'nothing needs attention'>"}. close_day.sh sends that line; it is the day's only Telegram message.

The very last thing in your response is the bug-tracker block described in
.claude/skills/bug-tracker/SKILL.md ("Unattended runs"): a fenced code block with the info
string bug-tracker holding a JSON list (empty list if nothing applies). This script applies it.

Your report is written to $OUT only after you finish, so that file is empty or missing while you
run. That is expected: do not report it as a failure.
PROMPT_EOF

echo "=== EOD Claude review $DAY ==="
# Every attempt keeps its own files, never overwritten:
#   reports/review-attempts/<day>-<HHMMSS>.out   what the CLI wrote to stdout
#   reports/review-attempts/<day>-<HHMMSS>.err   what it wrote to stderr
#   reports/review-attempts/<day>-<HHMMSS>.exit  its exit code (absent: the attempt was killed)
#   reports/review-attempts/<day>-latest.txt     a one-line pointer to the newest attempt id
# A pointer file, not a symlink: symlinks do not survive the Windows reader of the WSL share.
# The exit code is diagnostic evidence only; signal_engine/ops/verdict.py reads the verdict.
ATTEMPTS="$REPORTS/review-attempts"
mkdir -p "$ATTEMPTS"
ATTEMPT="$DAY-$(date +%H%M%S)"
n=1
while [ -e "$ATTEMPTS/$ATTEMPT.out" ] || [ -e "$ATTEMPTS/$ATTEMPT.exit" ]; do n=$(( n + 1 )); ATTEMPT="$DAY-$(date +%H%M%S)-$n"; done
: > "$ATTEMPTS/$ATTEMPT.out"
: > "$ATTEMPTS/$ATTEMPT.err"
printf '%s\n' "$ATTEMPT" > "$ATTEMPTS/$DAY-latest.txt.tmp.$$" && mv -f "$ATTEMPTS/$DAY-latest.txt.tmp.$$" "$ATTEMPTS/$DAY-latest.txt"
write_exit() { printf '%s\n' "$1" > "$ATTEMPTS/$ATTEMPT.exit.tmp.$$" && mv -f "$ATTEMPTS/$ATTEMPT.exit.tmp.$$" "$ATTEMPTS/$ATTEMPT.exit"; }
# The report keeps its old shape: stderr (warnings such as a sign-in expiry notice) then stdout.
compose() { { cat "$ATTEMPTS/$ATTEMPT.err"; cat "$ATTEMPTS/$ATTEMPT.out"; } > "$OUT.partial"; }
CLAUDE_PID=""
on_term() {  # the caller's deadline: keep what was written, as a .partial that the caller finishes
  [ -n "$CLAUDE_PID" ] && kill -TERM "$CLAUDE_PID" 2>/dev/null
  compose
  write_exit 143
  exit 143
}
trap on_term TERM INT
# --model is pinned: without it the CLI resolved Haiku on 2026-10-31, 11-02 and 11-03, and those
# runs skipped mandatory work. The review needs a consistent reasoning level.
"$CLAUDE_BIN" -p "$PROMPT" \
  --model sonnet \
  --permission-mode plan \
  --disallowedTools Edit,Write,NotebookEdit \
  --dangerously-skip-permissions \
  --output-format text \
  > "$ATTEMPTS/$ATTEMPT.out" 2> "$ATTEMPTS/$ATTEMPT.err" &
CLAUDE_PID=$!
wait "$CLAUDE_PID"
status=$?
trap - TERM INT  # the attempt is over: a later signal must not rebuild a .partial
write_exit "$status"
compose
# A report from an earlier attempt is kept next to the attempt files before it is replaced.
[ -f "$OUT" ] && cp -p "$OUT" "$ATTEMPTS/$ATTEMPT.previous-report.md"
mv -f "$OUT.partial" "$OUT"  # whole report or none: a reader never sees it half-written
echo "report -> $OUT (exit $status, attempt $ATTEMPT)"

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
