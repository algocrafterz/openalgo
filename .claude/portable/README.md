# Portable Claude Code workflow

Goal: consistent Claude Code behaviour on any account or machine that has this repository checked out.
Parity is verified by `bootstrap.sh`, not assumed.

## What is shared and what is not

| Item | Where it lives | Travels with the repo? |
| --- | --- | --- |
| Project instructions | `CLAUDE.md` | Yes (tracked) |
| Project skills | `.claude/skills/` | Yes (tracked) |
| Project hook scripts | `.claude/hooks/` | Yes (tracked) |
| Project hook registration | `.claude/settings.json` | No: gitignored. Generated from `settings.project.template.json` by `--apply` |
| User rules and `~/.claude/CLAUDE.md` | `~/.claude/` | No. Canonical copy is `user/` here; `--apply` installs it |
| ECC plugin | `~/.claude/plugins/` | No. Installed with `claude plugin install` |
| User `settings.json` (model, env, hooks, theme) | `~/.claude/settings.json` | No. Checked, never edited. Contains machine paths |
| Login / credentials | `~/.claude/.credentials.json` | Never. Log in with `claude` per account |
| Auto memory (preferences, project notes) | `~/.claude/projects/<path>/memory/` | No. Per machine and per checkout path |
| claude.ai connectors (Gmail, Notion, ...), MCP auth | account / `~/.claude/.mcp.json` | No. Per account |
| `.claude/settings.local.json`, `.claude/RESUME.md` | local | No (ignored) |

On ONE machine, switching Claude accounts (`/logout`, `/login`) keeps everything under `~/.claude/` except the
login itself. The bootstrap matters for a NEW machine or OS user, or after `~/.claude` is wiped.

## Usage

```
.claude/portable/bootstrap.sh            # report parity, change nothing (exit 1 on any FAIL)
.claude/portable/bootstrap.sh --apply    # install what is missing; never overwrites a differing file
.claude/portable/bootstrap.sh --apply --force   # replace differing user files; old copy goes to ~/.claude/backups/portable-<ts>/
```

`CLAUDE_HOME=/some/dir` points it at another config directory. `--skip-plugin` skips the plugin CLI.

## Editing rules

The repo copy in `user/` is canonical for user-level rules. Edit it there, then run `--apply --force`.
Editing only `~/.claude/rules/` makes the next `--check` report drift. `user/` is not inside any
auto-loaded path, so the rules are loaded once (from `~/.claude/`), not twice.

## New account checklist

1. Install Claude Code, run `claude`, and log in with the new account (manual; never scripted).
2. `.claude/portable/bootstrap.sh --apply` (installs the ECC plugin, user rules, project hook registration).
3. Set the user `settings.json` keys it reports as WARN: `model`, `env.CLAUDE_CODE_SUBAGENT_MODEL`,
   `env.MAX_THINKING_TOKENS`, `enabledPlugins`. Use `/config` or edit the file.
4. Re-connect per-account connectors and MCP servers by hand if needed.
5. `.claude/portable/bootstrap.sh` again: expect 0 failures.
6. In a session, `/memory` lists the loaded instruction files and `/doctor prompt-audit` finds conflicts.

## Limits

- Model availability depends on the plan. A `sonnet` alias can resolve to a different model version per account.
- Instruction files are context, not enforced configuration. Only hooks enforce behaviour.
- The ECC plugin version is not pinned here (last used: 2.2.0).
- Auto memory is not synced; copy `~/.claude/projects/<path>/memory/` by hand if wanted.
