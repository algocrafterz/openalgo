#!/usr/bin/env bash
# Tier-1 auto-fix: after the market, take ONE low-risk open bug, fix it on a side branch, prove
# it and commit it there. Nothing is merged, pushed or restarted, and nothing is sent to Telegram.
#
# WHAT IT MAY FIX (policy in signal_engine/bugs/autofix.py, enforced after the run, not trusted):
#   LOW/MEDIUM bugs of kind BUG or DATA in area ops, data or signal_engine, where the change stays
#   in operations, reporting, tracker and test code. Entries, exits, stops, sizing, order paths,
#   risk rules, config, migrations and the OpenAlgo core are refused: those are Tier 2 (/resolve).
#
# HOW
#   1. pick      the next eligible bug (MEDIUM first, then most days seen)
#   2. worktree  ../openalgo-autofix/<BUG>, new branch autofix/<BUG> from this repo's HEAD
#   3. fix       headless Claude (Sonnet) in the worktree: test first, then the fix, targeted tests
#   4. gate      scope check on the diff; the new tests must FAIL without the fix and PASS with it;
#                ruff clean. Any miss discards the branch and notes why on the bug.
#   5. commit    on autofix/<BUG> only; the bug stays in_progress with the commit named, because a
#                fix on an unmerged branch is not running code
# The outcome is in this script's output, the run log and the bug's tracker notes.
# The owner merges (git merge autofix/<BUG>) and then marks the bug fixed. A bug is attempted at
# most twice.
#
# USAGE
#   ./signal_engine/analysis/autofix.sh               # next eligible bug
#   ./signal_engine/analysis/autofix.sh BUG-014       # that bug (must still be eligible)
#   AUTOFIX_DRY=1 ./signal_engine/analysis/autofix.sh # show what would be attempted
#
# CRON: none in the daily schedule. The 16:30 line is removed (nothing routine runs after 16:30
# IST); until the maintainer removes it from the crontab the line still works. Run this script by
# hand, or add a line of your own outside the 09:00-09:15 and 15:30-16:30 windows.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO" || { echo "FATAL: cannot cd to $REPO"; exit 1; }

UV="$(command -v uv || echo /home/anand/.local/bin/uv)"  # cron has a minimal PATH
CLAUDE_BIN="${AUTOFIX_CLAUDE_BIN:-$(command -v claude || echo /home/anand/.local/bin/claude)}"  # override: pipeline tests
# The tracker helpers always run against the main checkout, whatever PYTHONPATH the
# worktree steps below export.
PY=(env -C "$REPO" PYTHONPATH="$REPO" "$UV" run python)

# Market hours and the weekend: never compete with the live engine for CPU or the Claude quota.
NOW="$(date +%s)"
if [ -z "${AUTOFIX_ANYTIME:-}" ] && [ "$(date +%u)" -le 5 ] \
   && [ "$NOW" -ge "$(date -d 08:40 +%s)" ] && [ "$NOW" -lt "$(date -d 16:10 +%s)" ]; then
  echo "$(date +%F\ %T) autofix: market hours - not running."
  exit 0
fi

mkdir -p "$REPO/signal_engine/logs/autofix"
exec 9>"$REPO/signal_engine/logs/autofix.lock"
flock -n 9 || { echo "autofix: another run holds the lock."; exit 0; }

# An unattended job outlives an interactive sign-in. If the owner saved a long-lived token from
# `claude setup-token` in this file (mode 600, outside the repo), use it. It is never printed.
TOKEN_FILE="${CLAUDE_TOKEN_FILE:-$HOME/.config/openalgo/claude_token}"
if [ -r "$TOKEN_FILE" ]; then CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"; export CLAUDE_CODE_OAUTH_TOKEN; fi

[ -x "$CLAUDE_BIN" ] || { echo "FATAL: claude CLI not found"; exit 1; }

BUG="${1:-}"
if [ -z "$BUG" ]; then
  BUG="$("${PY[@]}" -m signal_engine.bugs.autofix pick)"
fi
if [ -z "$BUG" ]; then
  echo "$(date +%F\ %T) autofix: no eligible bug."
  exit 0
fi
if [ -n "${AUTOFIX_DRY:-}" ]; then
  echo "autofix (dry run): would attempt $BUG"
  "${PY[@]}" -m signal_engine.bugs show "$BUG"
  exit 0
fi

BRANCH="autofix/$BUG"
WT="$(dirname "$REPO")/openalgo-autofix/$BUG"
LOG="$REPO/signal_engine/logs/autofix/$BUG-$(date +%Y%m%d-%H%M%S).log"
echo "=== autofix $BUG $(date +%T) (log $LOG) ==="

cleanup_worktree() {
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"
  git -C "$REPO" worktree prune
}

give_up() {  # give_up REASON: discard the branch, reopen the bug, say why (stdout and tracker note)
  local reason="$1"
  cd "$REPO" || true  # the work area is removed below; never run from inside it
  echo "autofix $BUG gave up: $reason"
  cleanup_worktree
  git -C "$REPO" branch -D "$BRANCH" >/dev/null 2>&1 || true
  "${PY[@]}" -m signal_engine.bugs update "$BUG" --status open \
    --note "autofix gave up: $reason (log signal_engine/logs/autofix/$(basename "$LOG"))" >/dev/null
  exit 1
}

# Start: record the attempt (counted by the policy) and take the bug out of the queue.
"${PY[@]}" -m signal_engine.bugs update "$BUG" --status in_progress \
  --note "autofix attempt: started $(date +%F\ %T)" >/dev/null \
  || { echo "autofix: could not mark $BUG in progress"; exit 1; }

if git -C "$REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  give_up "the branch $BRANCH already exists"
fi
MAIN_BEFORE="$(git -C "$REPO" status --porcelain | grep -v 'signal_engine/bugs/bugs.json' | sort)"
mkdir -p "$(dirname "$WT")"
git -C "$REPO" worktree add -q -b "$BRANCH" "$WT" HEAD >>"$LOG" 2>&1 || give_up "could not create the work area"

# Tests need the same environment file as the main checkout; link it rather than copy it. It is
# git-ignored, so it is never staged, and the run is told not to open it.
[ -f "$REPO/.env" ] && ln -s "$REPO/.env" "$WT/.env"

SHOWN="$("${PY[@]}" -m signal_engine.bugs show "$BUG" 2>&1)"
read -r -d '' PROMPT <<PROMPT_EOF || true
You are fixing ONE confirmed bug in the OpenAlgo signal_engine, unattended, in this git worktree
(branch $BRANCH). Read CLAUDE.md first. The bug, from the tracker:

$SHOWN

Rules, all enforced by a gate after you finish; a break discards your work:
1. Write the regression test FIRST, from the evidence above, and run it to see it fail for the
   reason the bug describes. Put it in signal_engine/tests/ (or test/ for blueprint code). Add
   new tests or new lines only; never edit or delete an existing test line.
2. Then make the smallest fix. Allowed: signal_engine/ops/, signal_engine/analysis/ (not
   breakingtrade/), signal_engine/bugs/store.py and __main__.py, blueprints/ops_*.py and
   blueprints/bug_tracker.py, and tests. Anything else (entries, exits, stops, sizing, order
   paths, services/risk, config files, migrations, broker code, the OpenAlgo core, frontend) is
   forbidden. If the real fix needs a forbidden file, stop and say so in one sentence.
3. At most 8 files and 300 changed lines. Do not touch bugs.json or the tracker.
4. Run only targeted tests: uv run pytest <files> -q -p no:cacheprovider. Run uv run ruff check
   and uv run ruff format on files you changed. Python style per CLAUDE.md: loguru logger in
   signal_engine, no print, no emoji or unicode in log lines, no new dependencies.
5. Never open, print or edit .env or any credential file. Do not commit, push, merge or run git commands that change state. The script commits.
6. If you cannot reproduce the bug from the evidence, change nothing and say why.
Finish with a two-sentence summary: the cause, and what you changed.
PROMPT_EOF

(
  cd "$WT" || exit 1
  export PYTHONPATH="$WT" UV_PROJECT_ENVIRONMENT="$REPO/.venv" UV_NO_SYNC=1
  timeout --kill-after=15 1500 "$CLAUDE_BIN" -p "$PROMPT" \
    --model sonnet \
    --permission-mode acceptEdits \
    --allowedTools "Read,Edit,Write,Glob,Grep,Bash(uv run pytest:*),Bash(uv run ruff:*),Bash(git diff:*),Bash(git status:*),Bash(git log:*),Bash(ls:*)" \
    --disallowedTools "Read(./.env),Edit(./.env),Write(./.env),Bash(git commit:*),Bash(git push:*),Bash(git checkout:*),Bash(git reset:*),Bash(git stash:*),Bash(rm:*)" \
    --output-format text
) >>"$LOG" 2>&1
RC=$?
[ "$RC" -eq 0 ] || give_up "the Claude run ended with exit $RC (usage limit, sign-in or time limit)"

cd "$WT" || give_up "the work area vanished"
git add -A >>"$LOG" 2>&1

# Gate 1: scope.
NUMSTAT="$(git diff --cached --numstat)"
PROBLEMS="$(cd "$REPO" && printf '%s\n' "$NUMSTAT" | "${PY[@]}" -m signal_engine.bugs.autofix check)" \
  || give_up "outside what an unattended fix may change: $(echo "$PROBLEMS" | tr '\n' ';')"

export PYTHONPATH="$WT" UV_PROJECT_ENVIRONMENT="$REPO/.venv" UV_NO_SYNC=1
mapfile -t TESTS < <(git diff --cached --name-only --diff-filter=AM | grep -E '^(signal_engine/tests|test)/test_.*\.py$')
mapfile -t SOURCES < <(git diff --cached --name-only | grep -vE '^(signal_engine/tests|test)/')
[ "${#TESTS[@]}" -gt 0 ] || give_up "no test file was added or changed"

# Gate 2: the tests must fail without the fix (and not merely find nothing to run).
if [ "${#SOURCES[@]}" -gt 0 ]; then
  git stash push -q -- "${SOURCES[@]}" >>"$LOG" 2>&1 || give_up "could not set the fix aside to test the tests"
  "$UV" run pytest "${TESTS[@]}" -q -p no:cacheprovider -x >>"$LOG" 2>&1
  BASE_RC=$?
  git stash pop -q >>"$LOG" 2>&1 || give_up "could not restore the fix after the fail-first check"
  if [ "$BASE_RC" -eq 0 ] || [ "$BASE_RC" -eq 5 ]; then
    give_up "the new test passes without the fix, so it does not prove the bug"
  fi
fi

# Gate 3: with the fix the tests pass, and the code is clean.
"$UV" run pytest "${TESTS[@]}" -q -p no:cacheprovider >>"$LOG" 2>&1 || give_up "the tests fail with the fix"
mapfile -t PYFILES < <(git diff --cached --name-only --diff-filter=AM | grep '\.py$')
"$UV" run ruff check "${PYFILES[@]}" >>"$LOG" 2>&1 || give_up "ruff reports problems"
"$UV" run ruff format --check "${PYFILES[@]}" >>"$LOG" 2>&1 || give_up "ruff format reports problems"

# Gate 4: the run touched nothing outside its work area (the tracker file aside).
MAIN_AFTER="$(git -C "$REPO" status --porcelain | grep -v 'signal_engine/bugs/bugs.json' | sort)"
[ "$MAIN_BEFORE" = "$MAIN_AFTER" ] || give_up "the main working tree changed during the run"

TITLE="$("${PY[@]}" -c "from signal_engine.bugs import store; import sys; print(next(b['title'] for b in store.load()['bugs'] if b['id']==sys.argv[1]))" "$BUG")"
git add -A  # stash pop restores the fix to the work tree but not to the index
git commit -q -m "fix(signal_engine): $BUG $TITLE" >>"$LOG" 2>&1 || give_up "git commit failed"
SHA="$(git rev-parse --short HEAD)"
FILES="$(git show --name-only --format= HEAD | tr '\n' ' ')"
cd "$REPO" || exit 1
cleanup_worktree

"${PY[@]}" -m signal_engine.bugs update "$BUG" --status in_progress --commit "$SHA" \
  --fix "Unattended fix on branch $BRANCH, not merged: $FILES" \
  --note "autofix ready: $BRANCH @ $SHA; merge it, then mark fixed" >/dev/null
echo "autofix $BUG ready on $BRANCH @ $SHA"
