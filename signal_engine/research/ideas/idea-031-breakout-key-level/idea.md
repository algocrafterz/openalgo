---
id: IDEA-031
title: BREAKOUT key-level breakout and retest
stage: backtest
research_state: needs_more_evidence
strategy_category: Breakout
strategy_tags: [key-level, retest, value-area, initial-balance, intraday]
horizon: intraday
backtest_module: backtest/strategies/breakout.py
analysis_doc: strategy_library/intraday/orb/BREAKOUT-SPEC.md
source:
  type: self
  author: owner
  ref: _inbox/breakout-key-level.txt
  captured: 2026-10-08
summary: Intraday long and short entries on the retest of a broken previous-session value area, previous-day or first-hour level, 09:45 to 11:45 signal bars, structural stop, target at the next registered level (TP1 only), time exit at the 14:45 open.
revisit_when: After the shared trade-policy steps S1 to S10 are implemented and tested, so that the first recorded v1.0 run describes the frozen specification. Then record one v1.0 run.
last_reviewed: 2026-10-08
---

# IDEA-031 - BREAKOUT key-level breakout and retest

Status: v1.0 specification FROZEN on 2026-10-08. No v1.0 run has been recorded. The code does not yet
implement every frozen rule (see "Code gap" below), so a run today would describe a different strategy.

The specification is `strategy_library/intraday/orb/BREAKOUT-SPEC.md` and is the authority. The Pine
file `breakout.pine` is a migration oracle only; a Pine difference is classified and never changes the
specification. The machine-readable form is `strategy.yaml`; `spec.yaml` carries the same rules in the
intake schema.

## What this idea is

The owner's Pine strategy BREAKOUT, ported to Python (`strategy_core/breakout.py`), so that backtest,
paper and live use one implementation. Python is the canonical implementation. Pine stays as the
oracle until the migration gates pass (parity observed at least 95 percent and Wilson lower bound at
least 90 percent over at least 20 sessions and a meaningful signal count; raw numbers always kept).
Retiring Pine is not live authorization.

## Provenance: earlier variants are kept apart from the v1.0 evidence

Owner rule (2026-10-08): count every earlier variant for trial history, and keep them clearly
separated from the final v1.0 evidence so they do not inflate it.

- `disclosure.prior_variants: 17` in `spec.yaml` is a floor counted from memory and `breakout.md`, not
  from a ledger: the 2026-09-30 shared-core baseline (1), its value-area rerun (1), the 13 exit-plan
  variants, the 2026-10-08 whole-universe run made for the gap-filter evidence (1) and the 2026-10-08
  stamps-cleared check (1). Stop-width and per-setup breakdowns were analyses of one run, not
  variants. The count only rises if an earlier variant is found.
- These variants raise the trial count and the significance hurdle. They are history. They are never
  merged into, averaged with or cited as evidence for the v1.0 run record.
- The v1.0 evidence is the first recorded v1.0 run only.
- Earlier result for orientation only: the 2026-09-30 baseline (TP1 only, 16 bps) had gross about
  -0.015R and net about -0.48R per trade, no gross edge. It is not a v1.0 result and is not evidence
  for or against v1.0.

## Known Pine defect: the gap filter never ran

The Pine input `enableGapFilter` (2.5 percent) is dead code under the native session: the gap block
never ran after the first bar of history, so no signal was ever gap-filtered. v1.0 was frozen
WITHOUT a gap gate, so v1.0 is not a test of the gap filter and must not be described as one. A gap
gate, if ever wanted, is a new version with its own trial. Fixing or removing the Pine code is a
separate task after a TradingView check.

## Decision log (owner, 2026-10-07 and 2026-10-08)

- Counter-bias veto included in v1.0; exit plan tp1_only with the Ladder owning stop state; NR filter dropped.
- Break stamps reset at each IST session start (check: 99,116 trades per arm, zero changed).
- Re-entry cap 2 per symbol per direction per day on accepted filled entries; LONG, exit, LONG allowed.
- Retest break-bar quirk frozen; the corrected rule is v1.1.
- Bad fill: observe the fill, close at the fill-bar open if it breaks the condition (slippage over 0.5R,
  at or past TP1, or a gap through the stop), zero gross R, real loss, entry counts as used.
- Fill timing: TP at the close of the touching bar; time exit at the 14:45 open; DATA_GAP flagged and never evidence.
- Paper limits equal live limits, applied per strategy book; no same-side cap.
- Pine BREAKOUT dispatch stops on the BREAKOUT-PY universe during parity.
- Two-view reporting (economic view first, strategy view beside it); `INV` I3 and `ACC` G1.2 wording approved.
- Universe: a versioned deployment list supplied by the owner; the full F and O universe for research.

## Code gap (why no run is recorded yet)

Today's code differs from the frozen rules in these places, each fixed by a numbered step of the
shared-rule design in the specification (S1 to S10): counter-bias veto deferred in the core,
re-entry and per-direction cap not shared across modes, TP fill at the target price, time exit at the
bar close, no bad-fill close in the engine and SimSink, paper limits off, break stamps carry across
sessions. The accounting fixes (one close, one ledger row) are already in the working tree.

## Revisions waiting

v1.1 retest fix (break at least one bar before the retest), a gap gate if evidence supports it,
afternoon window, other exit plans. Each is a separate trial.
