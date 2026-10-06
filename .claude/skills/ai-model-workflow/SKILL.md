---
name: ai-model-workflow
description: Choose which Claude model does which phase of a task (`opus` and `sonnet` aliases only), and how to hand work between them. Use before starting any task that is more than a simple, well-defined change, before spawning any subagent, or when asked about model selection. Applies most to trading or backtest logic, financial calculations, broker or order execution, migrations, security, and architecture.
---

# AI Model Workflow

Opus is the decision and review model. Sonnet is the execution model.
Do not optimize for model usage. Optimize for reasoning quality, speed, and confidence.

## Allowed models

- Use only the `opus` and `sonnet` model aliases.
- Do not pin model version IDs.
- Do not use Haiku or other models for this workflow.
- Sonnet is the default. Pass `model` explicitly when you spawn a subagent.

## Decision rule

Ask one question: is deciding WHAT to do substantially harder than implementing HOW to do it?

```text
Simple, well-defined task
  -> Sonnet

Non-trivial task where deciding WHAT to do needs meaningful reasoning
  -> Opus: investigate and design
  -> Sonnet: implement and test
  -> Opus: independent review, when the change warrants it
```

If the answer is no, use Sonnet directly. Do not add a phase for ceremony.

Signs that WHAT is hard: ambiguous requirements, several reasonable designs,
several interacting components, unfamiliar code, an expensive wrong decision.

## Plan size

Match the plan to the task.

| Task | Plan |
| --- | --- |
| Trivial | No Opus phase, no plan |
| Straightforward bug, root cause known | Objective, files, constraints, acceptance criteria |
| Small multi-file change | Concise plan: objective, approach, files, tests |
| Architecture or design work | Detailed plan: add current and desired behavior, root cause, alternatives, edge cases, out of scope |

Write a plan file (`docs/plans/<change>.md`) only when the work crosses phases or sessions,
or when the plan has durable value. Do not require one for a small task.
For a small change, a plan in the subagent prompt is enough.
For trading changes, state the old behavior and the new behavior.

## Phases

**Reasoning (Opus).** Read the relevant code. State current and desired behavior.
Find the root cause when fixing a defect. Choose the simplest design that meets the requirements.
Record risks and edge cases. Label exploratory ideas so they do not become requirements.

**Implementation (Sonnet).** Treat the plan as the contract. Make the smallest correct change.
Keep existing behavior, APIs, and naming unless the plan changes them.
Do not redesign. Run the relevant tests. Report what changed and what ran.
If the plan conflicts with the code, or a significant issue appears, stop and escalate.

**Review (Opus).** Compare the diff to the plan. Check correctness, requirements coverage,
edge cases, regression risk, test coverage, unintended changes, and scope.
List concrete issues by severity. Do not modify code unless asked.

After implementation, the orchestrator also reads the full diff and runs the repository checks.
A successful compile is not a successful task.

## Opus review

Opus review is mandatory for:

- Trading or backtest logic
- Financial calculations
- Broker or order execution
- Market-data ingestion or transformation
- Database or data migrations
- Security-sensitive changes
- Architecture changes
- Changes with significant regression risk

For other non-trivial work, Opus review is recommended, not mandatory.
Skip it when it would cost more than the change. Do not force three phases on every change.

## Escalate to Opus

Sonnet stops and escalates when the plan conflicts with the code, a requirement is ambiguous
in a way that changes the design, a public API must change, the fix needs broad refactoring,
or the scope has grown. Use this format:

```text
Problem discovered:
Why the plan is insufficient:
Impact:
Options:
Recommendation:
```

Do not make a major design decision silently during implementation.

## Make it observable

For a non-trivial task, explicitly state which workflow is being used.
If Opus is intentionally skipped, briefly explain why. Examples:

```text
Workflow: Sonnet direct implementation.
Reason: The change is well-defined and has no architectural or design ambiguity.
```

```text
Reasoning phase: Opus
Plan written to: docs/plans/<change>.md
Implementation phase: Sonnet
Review phase: Opus
```

State each phase when it starts. Use the `Plan written to:` line only when a plan file is created.

## Spawning subagents

- A subagent does not see this conversation. Give it a self-contained prompt.
- Pass the plan or the plan file path. Do not rely on chat history.
- Do not start the Sonnet phase before the user approves the plan when the change
  alters trading behavior, security, or architecture.
  Do not require approval for ordinary implementation work.
- Send heavy backtest or sweep work to a subagent. Ask for a distilled conclusion, not raw output.
- If the main session already runs on the model a phase needs, do that phase directly.
