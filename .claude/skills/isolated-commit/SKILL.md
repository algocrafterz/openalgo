---
name: isolated-commit
description: Make one focused, owner-approved commit that contains only the named change, while other sessions have uncommitted work in the same tree - stage explicit paths only, verify the staged diff, run the required checks, write a Conventional Commit message, never push. Use when the user says "commit this", "approved for an isolated commit", "stage only the files you identified", "commit in separate focused commits", or "commit as-is".
---

# Isolated commit

One run: `/isolated-commit <what was approved>`; for example `/isolated-commit G6.8 files` or
`/isolated-commit parser NaN fix`.

## 0. Preconditions (stop if one fails)

- The user explicitly asked for a commit in **this** message or an approved plan. Approval for an
  earlier commit does not carry over.
- The current branch is not `main`. The deployment branch is `feature/optimize-signal-engine-v2.0.2.1`.
- Know the exact list of approved paths. If the scope is unclear, list the candidate paths and ask.

## 1. Find out what is in the tree

```bash
git status --short
git diff --stat
```

Sort every changed path into **in scope** or **foreign**. Foreign changes usually belong to another
session: run `ListAgents` if you are not sure. Never touch foreign changes: no `git stash`, `reset`,
`checkout --`, `restore`, or `clean`.

A file can hold both in-scope and foreign hunks. `git add -p` is interactive and not available here. Write
the in-scope hunks to a patch and stage it with `git apply --cached <patch>`, then check that the work tree
still holds the foreign hunks.

When two changesets overlap on the same lines (so no hunk boundary separates them), split by content:

1. Number the hunks with `git diff -U0 -- <file>` and classify each as A, B or overlapping. Write the manifest first.
2. Build file B1 = working file with the other changeset's hunks reversed (match by exact line text and assert one match; keep the file's line endings). Never edit the working file.
3. Stage it without touching the work tree: `sha=$(git hash-object -w --path=<f> <B1>)` then `git update-index --cacheinfo <mode>,$sha,<f>`.
4. Prepare the second commit in a separate index: `GIT_INDEX_FILE=<tmp> git add -- <explicit paths>`, then `git write-tree` for both indexes.
5. Prove each commit stands alone: `git archive <tree>` into a scratch dir, link `.venv`, `node_modules` and `db`, create `log/test`, then run the targeted tests, `tsc` and the build there. Check `git diff --name-only <tree2>` against the work tree: only the foreign paths may differ.
6. Re-check `git log -1` before finishing; other sessions commit while you work.
7. When the user has approved the exact split, do not re-verify it: stage the explicit list for commit 1, confirm the staged diff equals the prepared patch, commit, then repeat for commit 2 (reset nothing; use the separate index or `git apply --cached`). Report hashes, files, and the untouched foreign paths.

## 2. Stage explicit paths only

```bash
git add -- <path1> <path2> ...      # never: git add -A, git add ., git add -u, git commit -a
git diff --cached --stat
git diff --cached                    # read every hunk
```

Every staged hunk needs a reason. No debug code, temporary files, scratch probes, secrets, `.env`,
databases, logs or unrelated reformatting. Untracked new files of the change must be staged too.

## 3. Checks (run on the staged content)

| Change touches | Required check |
| --- | --- |
| Python | `uv run ruff check <staged .py files>` and `uv run ruff format --check <files>` |
| Behavior | targeted tests first, for every code change: `PYTHONPATH=. uv run pytest <files> -q -p no:cacheprovider` |
| Commit gate | Run the full suite (`signal_engine/scripts/run_full_tests.sh --wait`) and the gthread suite only when the CLAUDE.md risk rules under "Tests" require them. Skip a required suite only with the user's approval, and record the reason. Never call pytest on the whole tree directly. Run one full suite at a time per machine. |
| Root `test/` | the affected `test/` files |
| Frontend | `cd frontend && npx biome check <files>`, `tsc`, vitest and `npm run build`; never stage `frontend/dist` on a branch |
| DB, sockets, threads, subprocesses, caches | the `fd-audit` skill |
| Schema | a migration in `upgrade/` registered in `migrate_all.py` |
| Strategy entry/exit/SL/TP/filter logic | a dated entry in that strategy's `STRATEGY-LOG.md` (five fields) |
| signal_engine fix or feature | `signal_engine/docs/PRD.md` entry |
| OpenAlgo core outside `signal_engine/` | explicit owner approval, and a line in `docs/strategy-pnl-fork-modification.md` |

If a check fails, fix the cause or stop and report it. Never `--no-verify`, never skip or weaken a test.
If the user said "commit as-is", still run the checks and report the results; do not expand the scope.

## 4. Commit

```bash
git commit -F - <<'EOF'
<type>(<scope>): <what changed, imperative, <= 72 chars>

<why, 1-4 short lines; facts only>
EOF
```

- Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`. Scope is usually `signal_engine`.
- No attribution lines, no "generated by", no emojis (owner rule overrides harness attribution).
- One logical change per commit. If the approval named several, make several commits in the stated order.

## 5. After the commit

```bash
git show --stat HEAD
git status --short                   # foreign changes are still there, unchanged
```

Never push, open a PR, merge, or deploy unless asked in this message.

## 6. Report

```
Commit   <hash> <subject>
Files    <n>: <paths>
Checks   ruff: pass | targeted: <n> passed | full suite: <result | not required: reason | skipped: approved by user> | gthread: <same>
Left     foreign changes untouched: <paths>
Not done push; <anything skipped and why>
```
