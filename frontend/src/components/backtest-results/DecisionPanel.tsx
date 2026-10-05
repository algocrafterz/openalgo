import { useState } from 'react'
import type {
  DerivedVerdict,
  EvaluationBlock,
  NextAction,
  Promotion,
  ResearchState,
  RunVerdictValue,
  Stage,
  TradeSubset,
  Verdict,
  VerdictReason,
} from '@/api/backtestResults'
import { findGateRow } from '@/lib/backtestResults'
import { cn } from '@/lib/utils'
import { InfoTip } from './InfoTip'
import { decisionClass, decisionText } from './PromotionGate'
import { ShowTradesButton } from './ShowTradesButton'
import { VerdictBadge } from './VerdictBadge'

const VERDICT_LABEL: Record<RunVerdictValue, string> = {
  EDGE: 'Edge',
  MARGINAL: 'Marginal',
  NO_EDGE: 'No edge',
  INCONCLUSIVE: 'Inconclusive',
}

const VERDICT_CLASS: Record<RunVerdictValue, string> = {
  EDGE: 'border-green-500/60 bg-green-500/10 text-green-700',
  MARGINAL: 'border-amber-500/60 bg-amber-500/10 text-amber-700',
  NO_EDGE: 'border-red-500/60 bg-red-500/10 text-red-700',
  INCONCLUSIVE: 'border-border bg-muted text-muted-foreground',
}

const STATE_LABEL: Record<ResearchState, string> = {
  active: 'Active',
  promising: 'Promising',
  needs_more_evidence: 'Needs more evidence',
  weak_evidence: 'Weak evidence',
  evidence_against_edge: 'Evidence against an edge',
  shelved: 'Shelved',
  rejected: 'Rejected',
  blocked: 'Blocked',
}

const STAGE_LABEL: Record<Stage, string> = {
  inbox: 'Inbox',
  backtest: 'Backtest',
  paper: 'Paper',
  live: 'Live',
  retired: 'Retired',
}

const PROMOTION_STAGES = [
  ['backtest', 'Backtest'],
  ['paper', 'Paper'],
  ['live', 'Live'],
] as const

function gateId(reason: VerdictReason): string | null {
  if (reason.gate) return reason.gate
  const ref = reason.evidence_ref
  return ref?.includes(':') ? ref.split(':').slice(1).join(':') : null
}

function ReasonItem({
  reason,
  evaluation,
  onShowTrades,
}: {
  reason: VerdictReason
  evaluation?: EvaluationBlock | null
  onShowTrades?: (subset: TradeSubset) => void
}) {
  const [open, setOpen] = useState(false)
  const gate = gateId(reason)
  // A reason may name a group such as G2 while the evaluation rows are G2.1, G2.2.
  const target = gate ? (findGateRow(evaluation, gate)?.gate ?? gate) : null
  return (
    <li className="text-sm">
      <button
        type="button"
        className="text-left underline decoration-dotted"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {reason.text}
      </button>
      {gate && (
        <a className="ml-2 text-xs underline" href={`#gate-${target}`}>
          Gate {gate}
        </a>
      )}
      <ShowTradesButton gate={target ?? gate} onShowTrades={onShowTrades} />
      {open && (
        <div className="ml-4 text-xs text-muted-foreground">
          {reason.status ? `Gate status: ${reason.status}. ` : ''}
          {reason.evidence_ref ? `Evidence: ${reason.evidence_ref}` : 'No linked evidence row.'}
        </div>
      )}
    </li>
  )
}

function RunVerdictRegion({
  verdict,
  evaluation,
  onShowTrades,
}: {
  verdict: DerivedVerdict | null
  evaluation?: EvaluationBlock | null
  onShowTrades?: (subset: TradeSubset) => void
}) {
  return (
    <section
      className="rounded-md border p-3 space-y-2 flex-1 min-w-[16rem]"
      data-testid="run-verdict"
      aria-label="Run verdict"
    >
      <div className="text-xs text-muted-foreground">
        Run verdict (calculated from the evidence) <InfoTip term="run_verdict" />
      </div>
      {verdict ? (
        <>
          <span
            data-testid="verdict-badge"
            className={cn(
              'inline-block rounded-md border px-3 py-1 text-lg font-semibold',
              VERDICT_CLASS[verdict.run_verdict]
            )}
          >
            {VERDICT_LABEL[verdict.run_verdict] ?? verdict.run_verdict}
          </span>
          {(verdict.caps_applied ?? []).map((cap, i) => (
            <div key={`${cap}-${i}`} className="text-sm text-amber-700" data-testid="verdict-caps">
              Held at MARGINAL: {cap}
            </div>
          ))}
          {(verdict.reasons ?? []).length > 0 && (
            <ul className="space-y-1 list-disc pl-5" data-testid="verdict-reasons">
              {(verdict.reasons ?? []).map((r, i) => (
                <ReasonItem
                  key={`${r.text}-${r.evidence_ref ?? ''}-${i}`}
                  reason={r}
                  evaluation={evaluation}
                  onShowTrades={onShowTrades}
                />
              ))}
            </ul>
          )}
        </>
      ) : (
        <div className="text-sm">No verdict was calculated for this run.</div>
      )}
    </section>
  )
}

function StateRegion({ researchState, stage }: { researchState?: ResearchState; stage?: Stage }) {
  if (!researchState && !stage) return null
  return (
    <section
      className="rounded-md border p-3 space-y-1 min-w-[12rem]"
      data-testid="research-state"
      aria-label="Research state"
    >
      <div className="text-xs text-muted-foreground">
        Research state (set by hand) <InfoTip term="research_state" />
      </div>
      {researchState && <div className="font-medium">{STATE_LABEL[researchState]}</div>}
      {stage && (
        <div className="text-sm">
          <span className="text-muted-foreground">Stage: </span>
          {STAGE_LABEL[stage]}
        </div>
      )}
    </section>
  )
}

function PromotionSummary({ promotion }: { promotion: Promotion }) {
  return (
    <section className="space-y-1" data-testid="promotion-summary" aria-label="Promotion">
      <div className="text-xs text-muted-foreground">
        Promotion <InfoTip term="promotion_gate" />
      </div>
      <ul className="grid gap-2 sm:grid-cols-3">
        {PROMOTION_STAGES.map(([key, label]) => {
          const st = promotion.stages?.[key]
          const reasons = st?.reasons ?? []
          return (
            <li key={key} className="rounded-md border p-2" data-testid={`promotion-${key}`}>
              <div className="text-sm font-medium">{label}</div>
              <div className={cn('text-sm', decisionClass(st?.decision))}>
                {decisionText(st?.decision)}
              </div>
              {reasons.length > 0 && (
                <ul className="list-disc pl-4 text-xs text-muted-foreground">
                  {reasons.map((r, i) => (
                    <li key={`${r}-${i}`}>{r}</li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

const CALCULATED_TO_VERDICT: Record<RunVerdictValue, Verdict> = {
  EDGE: 'edge',
  MARGINAL: 'marginal',
  NO_EDGE: 'no-edge',
  INCONCLUSIVE: 'inconclusive',
}

/**
 * Verdict for a run with no captured detail. The idea-level verdict belongs to the latest run
 * only, so it is never shown as another run's.
 */
function LegacyVerdict({
  runVerdict,
  legacyVerdict,
  isLatestRun,
}: {
  runVerdict?: RunVerdictValue | null
  legacyVerdict?: Verdict
  isLatestRun?: boolean
}) {
  if (runVerdict) {
    return (
      <div className="text-sm" data-testid="legacy-verdict">
        Recorded verdict:{' '}
        <VerdictBadge verdict={CALCULATED_TO_VERDICT[runVerdict] ?? (runVerdict as Verdict)} />
      </div>
    )
  }
  if (isLatestRun && legacyVerdict && legacyVerdict !== 'none') {
    return (
      <div className="text-sm" data-testid="legacy-verdict">
        Latest run verdict: <VerdictBadge verdict={legacyVerdict} />
      </div>
    )
  }
  return (
    <div className="text-sm text-muted-foreground" data-testid="legacy-no-verdict">
      No calculated verdict was recorded for this run
    </div>
  )
}

/**
 * Decision first: the calculated run verdict, the researcher's own state (kept apart and
 * labelled differently), what each stage allows next, and the one thing to do now.
 */
export function DecisionPanel({
  verdict,
  promotion,
  nextAction,
  researchState,
  stage,
  legacyVerdict,
  runVerdict,
  isLatestRun,
  captured,
  evaluation,
  onShowTrades,
}: {
  verdict: DerivedVerdict | null
  promotion: Promotion | null
  nextAction: NextAction | null
  researchState?: ResearchState
  stage?: Stage
  /** The idea-level derived verdict. It describes the idea's latest run only. */
  legacyVerdict?: Verdict
  /** The selected run's own recorded verdict, when its record has one. */
  runVerdict?: RunVerdictValue | null
  /** Whether the selected run is the idea's latest run. */
  isLatestRun?: boolean
  captured: boolean
  evaluation?: EvaluationBlock | null
  /** Opens the trade drill-down on the subset behind a gate. */
  onShowTrades?: (subset: TradeSubset) => void
}) {
  if (!captured) {
    return (
      <section className="space-y-3 max-w-3xl" data-testid="decision-panel" aria-label="Decision">
        <div className="rounded-md border border-amber-500/60 bg-amber-500/5 p-3 space-y-2">
          <p className="text-sm" data-testid="legacy-note">
            This run predates calculated verdicts: verdict, promotion and next action were not
            captured.
          </p>
          <LegacyVerdict
            runVerdict={runVerdict}
            legacyVerdict={legacyVerdict}
            isLatestRun={isLatestRun}
          />
        </div>
        <StateRegion researchState={researchState} stage={stage} />
      </section>
    )
  }

  return (
    <section className="space-y-3 max-w-3xl" data-testid="decision-panel" aria-label="Decision">
      <div className="flex flex-wrap gap-3">
        <RunVerdictRegion verdict={verdict} evaluation={evaluation} onShowTrades={onShowTrades} />
        <StateRegion researchState={researchState} stage={stage} />
      </div>
      {promotion && <PromotionSummary promotion={promotion} />}
      {nextAction && (
        <section
          className="rounded-md border p-3 space-y-0.5"
          data-testid="next-action"
          aria-label="Next action"
        >
          <div className="text-xs text-muted-foreground">Next action</div>
          <div className="font-medium">{nextAction.action}</div>
          <div className="text-sm text-muted-foreground">{nextAction.reason}</div>
        </section>
      )}
    </section>
  )
}
