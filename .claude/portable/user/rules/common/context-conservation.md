# Context & Token Conservation

Context was getting exhausted too fast across sessions. These are standing
rules to keep usage lean, not just a one-time cleanup.

## Prefer ECC when it clearly fits — don't force it

- If a request matches an existing `ecc:` skill, agent, or command (code
  review, TDD, refactor cleanup, security review, build-error fixing, etc.),
  use that instead of rolling an ad hoc approach — it's tuned for the task.
- Do NOT invoke an ECC skill/agent for trivial or simple requests just because
  one exists (typo fixes, one-line answers, quick lookups). Forcing a
  skill/subagent invocation on every request adds tool calls and context —
  the opposite of the goal here.
- See [[agents]] — its "Immediate Agent Usage" list is guidance for when a
  matching agent exists, not a mandate to spawn one for every request.

## Settings already applied (`~/.claude/settings.json`)

- `MAX_THINKING_TOKENS=10000` (down from the 31,999 default) — cuts hidden
  reasoning-token cost substantially; raise it per-session for genuinely
  complex architectural work if needed.
- `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` — subagents (Task tool) default to
  Sonnet. Pass `model: haiku` per spawn for pure file-reading/exploration.
- `model: sonnet` stays the session default; switch to `/model opus` only for
  genuinely hard reasoning.

## Working habits

- **Use subagents/forks for exploration, not your main context.** A fork or
  subagent can read 20 files and hand back a distilled summary — reading all
  20 directly in the main session burns context that never gets reclaimed.
- **Read narrowly.** Use `Grep`/targeted `Read` with `offset`/`limit` instead
  of dumping whole files when only a section is relevant.
- **Filter Bash output.** Pipe long-running or verbose commands (test runs,
  logs, backtests, `git log/diff`, `find`) through `head`, `tail`, `grep` or
  `wc -l`, or redirect to a file and read the relevant part. Never `cat` a
  large file; use `Read` with `offset`/`limit`. In one long session Bash
  results were 15% of context (152k tokens), the largest avoidable cost.
- **Batch independent tool calls** in one turn instead of sequential
  round-trips when there's no dependency between them.
- **`/clear` between unrelated tasks**; stale context costs tokens on every
  later message. **`/compact`** at logical breakpoints (after planning, after
  debugging, before switching focus) — not mid-implementation.
- **Keep MCP servers to what's actually used** (target under 10 enabled) —
  each one adds tool definitions to every request's context, whether or not
  it's used this session.
- **Don't duplicate installed content.** If a loose file under `~/.claude/`
  (an agent, skill, or command) duplicates something a plugin already
  provides, remove the duplicate rather than keeping both — see the 2026-09-20
  cleanup of `~/.claude/agents/*.md` duplicating `ecc:*` agents (originals kept
  in `~/.claude/backups/agents-loose-duplicates-2026-09-20/`).
