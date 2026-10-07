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

- Use only the `opus` and `sonnet` model aliases for this workflow.
- Do not pin model version IDs.
- Do not use Haiku or other lower-capability models for this workflow.
- Sonnet is the default implementation model.
- Pass `model` explicitly whenever spawning a subagent or ECC agent. Never rely on the
  environment's default subagent model for this workflow.
- If a phase needs Opus and the `opus` model is unavailable, do not silently substitute
  Sonnet. For a non-risky task, ask the user whether to proceed with Sonnet.
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

## Context and token efficiency

Treat context and token usage as resources.

- Do not read files, documentation, history, or tool output that is not relevant to the current decision or implementation.
- Prefer targeted repository search and the smallest sufficient file/range over broad scans.
- Reuse verified findings, plans, test results, and file references from earlier phases instead of rediscovering the same information.
- A subagent must receive the smallest self-contained context needed to perform its task. Do not pass the full conversation or unrelated repository material.
- Do not repeat research already completed by another phase unless verification is required or the earlier result is uncertain.
- Do not create a separate agent/task when the current agent can complete the work directly without meaningful loss of quality.
- Do not split work into subtasks merely to increase parallelism.
- Prefer one focused investigation over several overlapping investigations.
- Ask for distilled conclusions from research or heavy computation, not raw output.
- When a task is complete, stop. Do not perform speculative cleanup, additional research, or unrelated improvements.

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

## ECC coordination

ECC (`affaan-m/ECC`) is installed in this Claude Code environment and should be used
as the available coordination/tooling layer for complex workflows.

Before a substantial workflow:

1. Inspect the installed ECC version and relevant available skills/agents.
2. Check whether a newer ECC version provides a capability materially relevant to this task.
3. If a relevant update is available, report it before relying on the new capability.
4. Do not update ECC automatically during task implementation unless the user explicitly
   authorizes the update.
5. Do not install ECC again if it is already installed.

Use the most appropriate ECC skill/agent when it materially improves:
- research or repository exploration
- planning
- implementation
- TDD/test-driven development
- code review
- security review
- architecture review
- multi-agent coordination
- context/session handoff

Do not invoke ECC skills merely for ceremony. Prefer the smallest ECC capability or workflow
that provides a meaningful benefit.

Do not let ECC override this skill's model-selection or approval rules.
`ai-model-workflow` remains authoritative for:
- Opus vs Sonnet selection
- phase boundaries
- implementation handoff
- escalation
- mandatory Opus review
- user approval requirements

When an ECC orchestration workflow overlaps with this workflow, compose them rather
than running two competing pipelines. Avoid duplicate planning, implementation,
testing, or review phases.

Prefer individual ECC capabilities over a full ECC orchestration pipeline when
`ai-model-workflow` already defines the required phases. Use an ECC `orch-*`
workflow only when its additional coordination or gating provides a clear benefit.

Before invoking an ECC skill, determine what phase it belongs to and which model should
perform that phase. Pass the approved plan/context explicitly to the ECC agent or skill.

If an ECC skill proposes a model or workflow that conflicts with this skill, follow
`ai-model-workflow` and adapt the ECC workflow accordingly.

Record significant ECC usage in the observable workflow status so the user can see
how coordination was performed.

### Context handoff

When using ECC agents or subagents, prefer artifact-based handoff over conversational handoff.

Pass:
- task objective
- relevant constraints
- approved plan
- relevant file paths
- acceptance criteria

Return:
- conclusions
- decisions
- changed files
- test results
- unresolved issues
- artifact paths

Do not pass or return large raw outputs unless required for the next decision.

### ECC selection rule

Use ECC when it provides a concrete capability that materially improves the task
or avoids duplicating workflow logic in this repository.

Examples:

| Need | Preferred approach |
|---|---|
| Repository research/exploration | Relevant ECC research/navigation skill |
| TDD implementation | ECC TDD skill/agent + Sonnet implementation |
| Security-sensitive change | ECC security workflow + mandatory Opus review |
| Architecture/design | ECC architecture/planning capability + Opus |
| Code review | ECC review capability + Opus |
| Large multi-step workflow | Relevant ECC orchestration workflow |
| Simple one-file change | Do not invoke ECC unnecessarily |
| Trading/backtest behavior | Follow `ai-model-workflow`; use ECC capabilities where they improve research, testing, or review |

### ECC and model workflow

ECC coordinates capabilities; `ai-model-workflow` decides model responsibility.

For example:

    Opus
      -> determine problem and architecture
      -> create/approve plan

    ECC
      -> provide repository research / TDD / specialist / orchestration capability

    Sonnet
      -> implement approved plan
      -> run tests

    ECC
      -> provide relevant verification/review tooling

    Opus
      -> perform independent review for mandatory-review changes

Do not allow an ECC agent to silently turn a Sonnet implementation phase into an Opus phase,
or vice versa. The selected model must remain explicit and observable.