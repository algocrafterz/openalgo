---
name: ai-model-workflow
description: Choose which Claude model does which phase of a task (`opus` and `sonnet` aliases only), and how to hand work between them. Use before starting any task that is more than a simple, well-defined change, before spawning any subagent, or when asked about model selection. Applies most to trading or backtest logic, financial calculations, broker or order execution, migrations, security, and architecture.
---

# AI Model Workflow

Opus is the primary reasoning and review model for decisions that materially affect design,
behavior, or risk. Sonnet is the default implementation model. Sonnet makes normal
implementation decisions inside the approved design.
Do not optimize for model usage. Optimize for reasoning quality, implementation speed, and confidence.

## Allowed models

- Use only the `opus` and `sonnet` model aliases.
- Do not pin model version IDs.
- Do not use Haiku or other models for this workflow.
- Sonnet is the default. Pass `model` explicitly when you spawn a subagent.
- If a phase needs Opus and the `opus` model is unavailable, do not silently substitute Sonnet.
  For a non-risky task, ask the user whether to proceed with Sonnet.
  For a mandatory-review or high-risk phase, stop and wait for Opus.

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
For a small change, keep the plan in the implementation prompt. Do not create a plan file.
For trading changes, state the old behavior and the new behavior.

## Phases

**Reasoning (Opus).** Read the relevant code. State current and desired behavior.
Find the root cause when fixing a defect. Choose the simplest design that meets the requirements.
Record risks and edge cases. Label exploratory ideas so they do not become requirements.

**Implementation (Sonnet).** Treat the plan as the contract. Make the smallest correct change.
Keep existing behavior, APIs, and naming unless the plan changes them.
Do not redesign. Run the relevant tests. Report what changed and what ran.
If the plan conflicts with the code, or an escalation condition is met, stop and escalate.

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

"Significant regression risk" means a change that can affect several independent flows,
shared infrastructure, persistent data, production runtime behavior, or externally visible behavior.

For other non-trivial work, Opus review is recommended, not mandatory.
Skip it when the change is low-risk and the review cost is disproportionate to the likely benefit.
Do not force three phases on every change.

## Escalate to Opus

Sonnet must stop and escalate when implementation reveals a problem that changes the approved
design, requirements, externally visible behavior, public API, data semantics, security
properties, or architectural boundaries. This includes a plan that conflicts with the code,
a fix that needs broad refactoring, and scope that has grown.

Do not escalate for ordinary implementation details that can be resolved without changing
those things. Use this format:

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
- Do not start implementation before the user approves the plan when the change alters
  trading behavior, financial calculations, security, architecture, or externally visible
  execution semantics. Examples that need approval: a filter threshold, the stop-loss
  calculation, fill semantics, position sizing, the backtest cost model.
  Examples that do not: logging, refactoring inside one function, adding a test.
  Do not require approval for ordinary implementation work.
- Send heavy backtest or sweep work to a subagent. Ask for a distilled conclusion, not raw output.
- If the main session already runs on the model a phase needs, do that phase directly.
