# AI Model Workflow

This document defines which Claude model does which work in this repository.
`CLAUDE.md` holds the short pointer and the core decision rule. This file holds the detail.

## 1. Allowed models

Use only the latest Claude Opus and the latest Claude Sonnet.

- Do not use Haiku, Fable, or any older Opus or Sonnet version.
- Select models by alias (`opus`, `sonnet`) so the latest version is always used.
- Do not pin a dated or older model ID.
- Current latest at time of writing: Opus 5.5 (`claude-opus-5-5`), Sonnet 5.5 (`claude-sonnet-5-5`).
- Sonnet 5.5 is the default model. Do not replace it with a lower model such as Haiku.

Subagent default models can be set to a cheaper model in user settings
(for example `CLAUDE_CODE_SUBAGENT_MODEL`). Set that default to `sonnet`, never `haiku`.
Always pass the `model` parameter explicitly when you spawn a subagent.

## 2. Core decision rule

> Simple task: Sonnet.
> Complex task: Opus, then plan, then Sonnet, then tests, then Opus review.

If deciding WHAT to build is substantially harder than typing HOW to build it,
use Opus for the reasoning phase.

## 3. Choose the workflow

Use Sonnet only for simple, fully specified tasks. Examples:

- Rename a variable, fix a typo, add a null check
- Change a log message or a one-line config value
- Update a dependency version
- Add a simple unit test

Use Opus, then Sonnet, then Opus for all non-trivial work. This includes:

- Architecture and design decisions
- Complex debugging and unfamiliar code
- Shared libraries and data pipelines
- CI/CD and deployment logic
- Trading and backtesting logic
- Significant regression risk

When you are unsure, use Opus, then Sonnet, then Opus when two or more of these are true:

1. The problem is ambiguous.
2. There are several reasonable designs.
3. The change touches several components.
4. A wrong design is expensive to undo.
5. The code is unfamiliar or complex.
6. The regression risk is significant.
7. Another engineer would need a written explanation to implement it.

These areas always use the full workflow:

- Trading logic, backtesting, and risk controls
- Broker plugins, streaming, and the ZeroMQ or WebSocket pipeline
- Database schema changes and migrations
- Security-sensitive code, CI/CD, and deployment logic
- Shared libraries and performance-sensitive code

Do not add process to trivial work. Use the lightest workflow that gives enough confidence.

## 4. How to enforce it: one subagent per phase

The main session is the orchestrator. It does not do a phase itself when that phase
needs a different model from the one the main session runs on. It spawns a subagent
with the `Agent` tool and passes the model explicitly.

| Phase | Model | How |
| --- | --- | --- |
| A. Investigate, design, plan | Opus | `Agent` with `model: "opus"`; writes `docs/plans/<change>.md` |
| B. Implement, test | Sonnet | `Agent` with `model: "sonnet"`; reads the plan file |
| C. Validate | Orchestrator | Inspect the full diff, run tests and checks |
| D. Independent review | Opus | `Agent` with `model: "opus"`, read-only; reports findings |

Rules for the orchestrator:

- Give each subagent a self-contained prompt. A subagent does not see this conversation.
- Pass work between phases through the plan file, not through chat history.
- Do not let the Sonnet phase start until the user has approved the plan, when the change is
  architectural, security-related, or changes trading behavior.
- Heavy backtest or sweep work goes to a subagent. The subagent returns a distilled conclusion,
  not raw output.
- Ask the review subagent not to modify code unless the user asks for it.

If the main session already runs on the model a phase needs, it may do that phase
directly. It must still keep the plan file as the handoff.

## 5. Phase A: Opus design

Opus does the following before any code changes:

1. State the user's actual objective.
2. Read the relevant code and configuration.
3. Describe current behavior and desired behavior.
4. Find the root cause when fixing a defect.
5. Identify the patterns that must be preserved.
6. Compare alternatives only when the design is not obvious.
7. Choose the simplest solution that meets the requirements.
8. List risks, edge cases, and compatibility concerns.
9. Write the plan.

For trading changes, the plan must state the old behavior and the new behavior explicitly.

### Plan file format

Path: `docs/plans/<change>.md`. Include the sections that apply.

```markdown
# Implementation Plan

## Objective
## Current Behavior
## Desired Behavior
## Root Cause
## Requirements
## Design Decision
## Files / Components Affected
## Implementation Steps
## Existing Behavior to Preserve
## Constraints
## Edge Cases
## Tests
## Acceptance Criteria
## Out of Scope
```

Label each item as a requirement, a design decision, a suggestion, or an exploratory idea.
Exploratory ideas must not become requirements.

## 6. Phase B: Sonnet implementation

Prompt template for the Sonnet subagent:

```text
Read the approved implementation plan at docs/plans/<change>.md.
Treat the plan as the implementation contract.
Inspect the current codebase before you change anything.
Make the smallest set of changes that satisfies the plan.
Preserve existing behavior, APIs, naming, and conventions unless the plan changes them.
Do not redesign the solution.
If you find a contradiction, a missing requirement, or a significant technical problem,
stop and report it. Do not make architectural changes.
When finished: review the diff, run the relevant tests, fix implementation-level failures,
and report what changed, which tests ran, and any remaining concerns.
```

Sonnet may:

- Choose implementation details that fit the plan
- Fix compile and test failures
- Add the tests the plan requires
- Adapt to small differences between the plan and the actual code

Sonnet must not:

- Replace the approved architecture
- Expand the scope or refactor unrelated code
- Change public APIs without a reason in the plan
- Remove behavior because it looks unnecessary
- Modify tests only to make them pass

## 7. Escalate back to Opus

Sonnet stops and escalates when:

- The plan conflicts with the actual architecture.
- An ambiguous requirement changes the design.
- The change crosses several architectural boundaries.
- A public API must change.
- Existing behavior conflicts with the requested behavior.
- Tests show a deeper design problem.
- The fix needs broad refactoring.
- The task has grown beyond its scope.

Escalation format:

```text
Problem discovered:
Why the plan is insufficient:
Impact:
Options:
Recommendation:
```

Do not make a major architectural decision silently during implementation.

## 8. Phase C: validation

After implementation, the orchestrator does the following:

1. Inspect the complete diff.
2. Check that every change maps to the plan.
3. Run the relevant tests and the repository checks.
4. Check for unintended changes.
5. Check each acceptance criterion.

A successful compile is not a successful task.

## 9. Phase D: Opus review

For significant changes, an Opus subagent reviews the diff against the plan.
Review prompt:

```text
Review the implementation against the approved plan at docs/plans/<change>.md.
Do not redesign the solution for style reasons.
Check correctness, requirements coverage, architectural consistency, edge cases,
regression risk, test coverage, unintended changes, and scope discipline.
List concrete issues by severity. Do not modify code.
```

If the review finds issues, revise the plan or send fixes back to a Sonnet subagent.

## 10. Announce the transitions

For substantial work, say it clearly when the phase changes:

```text
Design complete. Plan written to docs/plans/<change>.md. Ready for implementation.
```

The model must not move from design to implementation only because it can.
