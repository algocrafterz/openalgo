# Unattended Tier-1 auto-fix (signal_engine/analysis/autofix.sh)

Runs weekdays 16:30 from cron. One low-risk bug per run, fixed on a side branch, never merged.
Policy lives in `signal_engine/bugs/autofix.py` (pure functions, tested); the shell script owns
git, the worktree and the Claude call. `AUTOFIX_DRY=1` shows the pick; `AUTOFIX_ANYTIME=1`
bypasses the market-hours guard; `AUTOFIX_CLAUDE_BIN`
swaps in a stub for pipeline tests.

## Tiers

| Tier | Scope | Who acts |
| --- | --- | --- |
| 0 | Recording (`ops track`) and notes | automatic |
| 1 | LOW/MEDIUM, kind BUG or DATA, area ops/data/signal_engine, change confined to ops, analysis (not breakingtrade), tracker store/CLI, blueprints/ops_*, tests | headless Claude on branch `autofix/BUG-nnn` |
| 2 | Anything touching entries, exits, stops, sizing, order paths, services/risk, config, migrations, broker code, core, or any HIGH bug | owner, via `/resolve` |

## Gates (enforced after the run, never trusted from the prompt)

1. Scope: allowed paths only, at most 8 files and 300 lines, no deleted lines in existing tests,
   at least one test added.
2. Fail-first: stash the non-test files, run the new tests; they must fail (exit 0 or 5 = reject).
3. Pass-with-fix, ruff check and ruff format clean.
4. The main checkout is unchanged (the tracker file aside).

A miss discards branch and worktree, reopens the bug with the reason as a note; a bug gets two
attempts (history notes starting `autofix attempt`). Success: commit on the branch, bug stays
`in_progress` with `--commit` set (an unmerged fix is not running code); the owner merges and
then marks it `fixed`.

## Pitfalls that cost time building it

- Run tracker helpers with `env -C <main repo> PYTHONPATH=<main repo>`. With cwd or
  PYTHONPATH in the worktree, `uv run python` imports the worktree's copy of the tracker module
  and reads its stale committed `bugs.json`.
- `git stash pop` restores the fix to the work tree but not the index: `git add -A` again before
  committing, or the commit holds only the test.
- A worktree has no git-ignored files. Tests need `.env`: symlink it from the main checkout and
  deny the run Read/Edit on it. Reuse the main `.venv` with `UV_PROJECT_ENVIRONMENT` and
  `UV_NO_SYNC=1` instead of syncing a second environment.
- After removing a worktree, `cd` out of it before running anything else, or uv fails with
  "Current directory does not exist".
- Headless call: `claude -p --model sonnet --permission-mode acceptEdits` with an explicit
  `--allowedTools` list (Read/Edit/Write/Glob/Grep plus `uv run pytest|ruff`, read-only git) and
  `--disallowedTools` for commit, push, checkout, reset, stash, rm. Verified that a bug the model
  judges not a defect ends with no change, which the scope gate reports as "changed nothing".
- Test the pipeline end to end with a stub Claude that applies a good change, an out-of-scope
  change and a test that passes without the fix; revert the throwaway tracker entries afterwards.
- Unattended jobs outlive an interactive sign-in: scripts read a long-lived token from
  `~/.config/openalgo/claude_token` (from `claude setup-token`) when the file exists.
