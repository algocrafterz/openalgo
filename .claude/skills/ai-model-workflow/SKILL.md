---
name: ai-model-workflow
description: Choose the least costly capable model and workflow for the task. Sonnet is the default. Use Opus only as a break-glass escalation for genuinely difficult or critical reasoning. Avoid unnecessary subagents, planning, review, documentation, and context usage.
---

# AI Model Workflow

Goal: correctness first, then speed and token efficiency. Use the least capable model that
can safely complete the task. Sonnet is the default. Opus is break-glass, not a normal phase.
Load this skill only when model selection, Opus escalation, user approval, or multi-phase
coordination matters. File count is not a complexity proxy.

## Models

- Sonnet: exploration, search, debugging with a discoverable root cause, implementation,
  tests, refactoring, logging, docs, UI, data processing, implementing an approved design,
  routine review, summaries.
- A cheaper model is fine for simple, repetitive, or retrieval tasks.
- Opus: only a genuinely hard reasoning problem where a cheaper model could plausibly get
  it wrong, such as:
  - materially different interpretations that repository evidence cannot resolve;
  - a novel architecture or algorithm decision with large consequences;
  - ambiguous trading, P&L, cost, fill, or execution semantics that matter;
  - a major data or migration decision that evidence cannot settle;
  - a genuine Sonnet reasoning dead-end;
  - the user asks for Opus.
- Trading, backtest, financial, market-data, database, migration, security, architecture,
  and broker work need strong validation. They do not by themselves need Opus.

## Opus discipline

- Uncertainty alone does not justify Opus.
- First resolve it from repository code, tests, authoritative documentation, and existing decisions.
- Do not escalate for ordinary implementation errors, obvious test failures, or incomplete code.
- Escalate only when the remaining problem is genuinely difficult or materially consequential.

Before invoking Opus, answer:

```text
Problem requiring Opus:
Why Sonnet or a cheaper model is insufficient:
Decision Opus must resolve:
Consequence if resolved incorrectly:
```

If you cannot answer clearly, do not invoke Opus. Opus decides; Sonnet implements.

## Subagents

- Use the least capable model that can do the task. An Opus parent does not imply Opus subagents.
- Opus subagent only with a stated reasoning problem, why Sonnet is insufficient, and the
  expected concise output. Never for search, exploration, tests, implementation, formatting,
  docs, or collecting information.
- Prefer direct execution. Do not split work to gain parallelism.
- Pass only: objective, constraints, file paths, approved plan, acceptance criteria.
  Return only: conclusions, decisions, changed files, test results, open issues, artifact paths.

## Paths

- Fast (clear, localized, low-risk, no semantic ambiguity): inspect, implement with Sonnet,
  run targeted tests, inspect the diff. No Opus, ECC, planning agent, or review agent.
- Normal (several interacting decisions): add a short in-context plan
  (Goal, Approach, Files, Tests). Write `docs/plans/<change>.md` only if the work crosses
  sessions, phases depend on it, or it records a durable architecture or semantics decision.
- Critical (meets the Opus gate): Opus resolves the question, record a concise decision,
  Sonnet implements, targeted tests, diff review.

## Trading and backtest changes

- No semantic change (tests, logging, equivalent refactor or speedup, formatting, known
  fixes): Sonnet.
- Semantic change with clear specification (from the request, approved plan, or repository
  convention): Sonnet.
- Semantic change with material ambiguity (entry/exit, sizing, SL/TP, fill timing, slippage,
  costs, P&L, look-ahead, dataset eligibility, execution): resolve from evidence, then Opus
  if still ambiguous. Never infer a new trading rule from implementation details.
- State old behavior, new behavior, and why. Approval is separate from model selection.
  Require user approval when trading semantics materially change, unless the user's request
  or an approved durable plan explicitly authorizes the new behavior. Existing tests
  establish current behavior only. They must not by themselves authorize a new trading rule.

## Review

Default: Sonnet implementation, targeted tests, final diff inspection. Use an independent
reviewer when it adds value. Opus review is optional: use it only when the change is
critical, tests cannot establish correctness, consequences are material, and independent
high-confidence review adds real value, or the user asks.

## ECC

Optional. Use the smallest relevant capability only when it adds a concrete benefit. Do not
check ECC versions, use it for simple implementation or routine tests, or run full
orchestration when direct execution suffices. ECC must not trigger an unnecessary Opus call.

## Context and communication

- Read only relevant files and ranges. Use targeted search. Filter large output. Reuse
  verified findings. Do not repeat research, run overlapping investigations, or duplicate reviews.
- Document only what has durable value: a design decision, a behavior change, or something
  repository policy requires. No ceremonial plans, reports, or summaries.
- Do not narrate routine work or announce the workflow. Default report: Changed, Verified,
  Result. Explain reasoning for significant decisions, behavior-affecting assumptions, test
  failures, escalations, or approvals. State why if Opus is used.

## Escalation and completion

Sonnet escalates when implementation reveals a problem that changes requirements, approved
behavior, public API, data semantics, security properties, architectural boundaries, or
critical trading semantics. Use: Problem, Why the current approach is insufficient, Impact,
Options, Recommendation. Resolve ordinary details from evidence. Never make a major design
decision silently.

Done when the behavior is implemented, relevant checks pass, the diff is inspected, needed
docs are updated, and no critical issue remains. Then stop. No speculative cleanup, extra
research, or extra review.
