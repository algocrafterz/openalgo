# Development Workflow

> This file extends [common/git-workflow.md](./git-workflow.md) with the full feature development process that happens before git operations.

The Feature Implementation Workflow describes the development pipeline: research, planning, TDD, code review, and then committing to git.

Scope: this pipeline applies to non-trivial features and behavior changes. Trivial work (typo, one-liner, config tweak) is inspect, change, verify. Use an agent only when delegation has a concrete benefit (isolated context, heavy output, independent review); see [agents.md](./agents.md) and [context-conservation.md](./context-conservation.md).

Precedence: if a repo CLAUDE.md conflicts with this file, follow the repo CLAUDE.md. This is guidance only. Claude Code concatenates both into context (project after user) and does not enforce precedence, so keep the two consistent.

## Feature Implementation Workflow

0. **Research & Reuse** _(before any non-trivial new implementation)_
   - **GitHub code search first:** Run `gh search repos` and `gh search code` to find existing implementations, templates, and patterns before writing anything new.
   - **Exa MCP for research:** Use `exa-web-search` MCP during the planning phase for broader research, data ingestion, and discovering prior art.
   - **Check package registries:** Search npm, PyPI, crates.io, and other registries before writing utility code. Prefer battle-tested libraries over hand-rolled solutions.
   - **Search for adaptable implementations:** Look for open-source projects that solve 80%+ of the problem and can be forked, ported, or wrapped.
   - Prefer adopting or porting a proven approach over writing net-new code when it meets the requirement.

1. **Plan First**
   - Use **planner** agent to create implementation plan
   - Keep the plan in the conversation; write planning docs only if the user asks
   - Identify dependencies and risks
   - Break down into phases

2. **TDD Approach**
   - Use **tdd-guide** agent
   - Write tests first (RED)
   - Implement to pass tests (GREEN)
   - Refactor (IMPROVE)
   - Verify 80%+ coverage

3. **Code Review**
   - Use ONE reviewer (**code-reviewer**, or the language reviewer such as **python-reviewer**; not both) after non-trivial code changes
   - Address CRITICAL and HIGH issues
   - Fix MEDIUM issues when possible

4. **Commit & Push**
   - Commit only when the user says so explicitly
   - Clean, concise messages in conventional commits format
   - See [git-workflow.md](./git-workflow.md) for commit message format and PR process
