---
name: status-report
description: Read-only status report on a workstream or the whole project - what is done, in progress (and by which branch or session), blocked on an owner decision, and not started - built from git, other sessions, memory, plan docs and the research registry, each item with evidence. Use when the user asks "status?", "where are we", "what is done / to do / in progress", "check status now and report", "summarise the current state of the backtest system / upgrade / paper run", or "what inputs do you need from me".
---

# Status report

One run: `/status-report [topic]`. Topic examples: `upgrade`, `backtest`, `paper`, `orphan-stops`,
`research`. With no topic, cover every open workstream.

Read-only. Do not fix, commit, merge, restart or delete anything while you build the report.

## 1. Gather (batch these calls; filter output)

```bash
git status --short
git worktree list
git branch --sort=-committerdate | head -15
B=feature/optimize-signal-engine-v2.0.2.1
for br in $(git branch --format='%(refname:short)' --sort=-committerdate | head -15); do
  echo "$br ahead=$(git rev-list --count $B..$br) merged=$(git merge-base --is-ancestor $br $B && echo y || echo n)"
done
git log --oneline -15 $B
```

- `ListAgents`: other live sessions and what they hold. Their worktrees: `git -C <path> status --short`.
- Memory: `MEMORY.md` index, then only the files that match the topic. Memory can be stale. Check each
  claim you repeat against git or the code, and say "per memory, not re-verified" when you cannot.
- Plans and state: `signal_engine/docs/*PLAN*.md`, `signal_engine/docs/PRD.md` (latest entries),
  `signal_engine/research/REGISTRY.md`, `signal_engine/docs/paper-monitoring/`, the latest
  `signal_engine/reports/eod-*-claude-review.md`.
- Engine health when the topic is paper or live: `signal_engine/data/runner_heartbeat`, today's log tail.

## 2. Classify each item

- **Done**: merged into the deployment branch (give the commit), or verified complete.
- **Ready, not deployed**: committed on a branch that is not merged (give branch and head).
- **In progress**: uncommitted work, or a live session holds it (give worktree or session name).
- **Blocked on owner**: needs an approval or a decision. Say the exact question, the options, and the
  effect of each option. Trading-behavior changes and cutovers always land here.
- **Not started**: agreed or recorded, with no code yet.
- **Risks**: things that will go wrong if nobody acts (stale branches, unpushed fixes, data gaps).

## 3. Output (chat; no file unless asked)

```
Status: <topic>, <YYYY-MM-DD HH:MM IST>

| Item | State | Evidence |
| --- | --- | --- |
| ... | Done / Ready / In progress / Blocked / Not started | commit, branch, file:line, session |

Decisions needed from you
1. <question> - options and effect

Risks
- ...

In plain terms
<3-6 sentences, no jargon>
```

Keep each Evidence cell short. Do not repeat what an item's own doc says; point to the doc.
