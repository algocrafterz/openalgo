#!/usr/bin/env bash
# Unattended daily /eod investigation - runs after eod.sh, REPORT-ONLY (never edits code).
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
# a human applies it later via /eod, same as every fix in signal_engine/PRD.md's 2026-09-21
# entries was applied - with review, tests, and a considered decision on scope each time.
#
# Runs 15 minutes after eod.sh's own 15:25 IST cron, giving it time to finish and OpenAlgo's
# end-of-day settlement to complete before the investigation reads trades.db/the tradebook.
#
# USAGE
#   ./signal_engine/analysis/eod_claude_review.sh            # today
#   ./signal_engine/analysis/eod_claude_review.sh 2026-09-08  # a specific session
#
# CRON (weekdays, 15:40 IST):
#   40 15 * * 1-5 /home/anand/github/openalgo/signal_engine/analysis/eod_claude_review.sh >> /home/anand/github/openalgo/signal_engine/logs/eod_claude_cron.log 2>&1

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

DAY="${1:-$(date +%F)}"
REPORTS="$REPO/signal_engine/analysis/reports"
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
Read .claude/skills/eod/SKILL.md in this repository and follow its step 1 (get today's report)
and step 2 (triage the regression check) exactly as written, for $DAY.

Skip step 3 (fix) entirely. This is an unattended, REPORT-ONLY run: Edit, Write, and
NotebookEdit are unavailable to you by design, and you must not attempt to work around that
(no writing files via shell redirection, no git commit, no database UPDATE/DELETE/INSERT, no
other mutating command of any kind). If you find a genuine, confirmed bug, describe it
precisely in your written response instead - root cause, the evidence you checked it against,
and the exact fix that would be needed - so a human can apply it later via /eod. Do not guess
at a root cause you have not actually verified against trades.db, the broker tradebook, or the
logs, the same standard the skill itself requires.

Finish with step 4's required two-part summary: a short technical summary, then a plain-English
layman recap with no jargon (trades taken, which strategy, won or lost how much, in terms a
non-technical reader gets immediately).
PROMPT_EOF

echo "=== EOD Claude review $DAY ==="
"$CLAUDE_BIN" -p "$PROMPT" \
  --permission-mode plan \
  --disallowedTools Edit,Write,NotebookEdit \
  --dangerously-skip-permissions \
  --output-format text \
  > "$OUT" 2>&1
status=$?

echo "report -> $OUT (exit $status)"
exit "$status"
