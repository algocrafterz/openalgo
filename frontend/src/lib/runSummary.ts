import type {
  CitedDiagnostic,
  EvaluationBlock,
  GateCorrection,
  GateRow,
  IdeaCard,
  NextAction,
  PortfolioView,
  PromotionDecision,
  ResearchState,
  RunDetail,
  RunRecord,
  RunVerdictValue,
} from '@/api/backtestResults'
import { fmtNum, fmtSigned } from './backtestResults'

/** A gate row with any evaluator correction applied, and the recorded row it replaced. */
export interface EffectiveRow extends GateRow {
  correction?: GateCorrection
}

/**
 * The gate rows the current reading uses: the sealed run's rows, with each gate that a later
 * evaluator fix re-scored replaced by its corrected values. The recorded row stays attached.
 */
export function effectiveRows(
  evaluation: EvaluationBlock | null | undefined,
  corrections: GateCorrection[] = []
): EffectiveRow[] {
  const byGate = new Map(corrections.map((c) => [c.gate, c]))
  return (evaluation?.dimensions ?? []).flatMap((d) =>
    (d.rows ?? []).map((row) => {
      const c = byGate.get(row.gate)
      if (!c) return row
      const value =
        typeof c.corrected.value === 'boolean' ? String(c.corrected.value) : c.corrected.value
      return { ...row, ...c.corrected, value, correction: c }
    })
  )
}

/** The first clause of a gate's evidence, short enough for a summary line. */
export function shortEvidence(evidence: string, max = 140): string {
  const first = evidence.split(/;\s|\.\s/)[0] ?? evidence
  return first.length > max ? `${first.slice(0, max - 3)}...` : first
}

export interface SummaryFinding {
  gate: string
  title: string
  text: string
  hard: boolean
  /** Set when the status shown comes from an evaluator correction, not the sealed record. */
  correctedFrom?: string
}

/** Passes that decide a strategy's case at the backtest stage, besides every hard pass. */
const DECISIVE_PASSES = new Set(['G2.3', 'G3.3', 'G3.4'])

function finding(row: EffectiveRow): SummaryFinding {
  return {
    gate: row.gate,
    title: row.title,
    text: shortEvidence(row.evidence),
    hard: row.hard,
    correctedFrom: row.correction?.changed_status ? row.correction.recorded.status : undefined,
  }
}

export const VERDICT_MEANING: Record<RunVerdictValue, string> = {
  EDGE: 'every gate this stage requires passed',
  MARGINAL: 'no hard gate failed, but a gate failed or required evidence is missing',
  NO_EDGE: 'a hard gate failed',
  INCONCLUSIVE: 'too little valid evidence to decide',
}

const STOPPED_STATES: ResearchState[] = ['rejected', 'shelved', 'evidence_against_edge']

export interface RecommendedAction {
  text: string
  why: string
  /** The platform's next open gate, shown as secondary when the researcher has stopped. */
  platform: NextAction | null
}

/** The idea fields the summary reads: the researcher's disposition and when to revisit. */
export type SummaryIdea = Pick<IdeaCard, 'research_state' | 'rejection_basis' | 'revisit_when'>

function recommendedAction(idea: SummaryIdea, detail: RunDetail): RecommendedAction {
  const platform = detail.next_action ?? null
  if (idea.research_state && STOPPED_STATES.includes(idea.research_state)) {
    return {
      text: 'Do not continue this version.',
      why: idea.revisit_when ?? `The research disposition is ${idea.research_state}.`,
      platform,
    }
  }
  return {
    text: platform?.action ?? 'No next action was recorded.',
    why: platform?.reason ?? '',
    platform: null,
  }
}

export interface RandomEntryComparison {
  strategy_bps: number
  random_bps: number
  excess_bps: number
  t_stat: number | null
  trades: number | null
}

function randomEntry(run: RunRecord): RandomEntryComparison | null {
  const b = run.baseline
  if (!b) return null
  return {
    strategy_bps: b.trade_net_bps ?? run.metrics.net_bps,
    random_bps: b.net_bps,
    excess_bps: b.excess_net_bps,
    t_stat: b.excess_t_stat ?? null,
    trades: b.trades ?? null,
  }
}

/**
 * One plain sentence when the book made money but the primary controls do not credit the
 * selection rule: matched random entry into the same stocks did better, or the out-of-sample
 * check failed. The equal-weight comparison is a supporting diagnostic and is kept out of it.
 */
function selectionNote(
  run: RunRecord,
  portfolio: PortfolioView | null,
  random: RandomEntryComparison | null,
  oosFailed: boolean
): string | null {
  const profitable =
    (portfolio?.stats?.total_return_pct ?? 0) > 0 || (run.metrics?.net_bps ?? 0) > 0
  if (!profitable) return null
  const parts: string[] = []
  if (random && random.excess_bps < 0) {
    parts.push(
      `random entry into the same stocks for the same holding periods made more (${fmtNum(random.random_bps, 0)} against ${fmtNum(random.strategy_bps, 0)} bps per trade, difference ${fmtSigned(random.excess_bps, 0)} bps${random.t_stat === null ? '' : `, t ${fmtNum(random.t_stat)}`})`
    )
  }
  if (oosFailed) parts.push('the out-of-sample result fell short of the criterion (G3.2)')
  if (parts.length === 0) return null
  return `The book made money, but ${parts.join(', and ')}. A profit alone does not show that the selection rule caused it.`
}

/** The equal-weight universe comparison, stated as supporting evidence only. */
function supportingNote(equalWeight: CitedDiagnostic | undefined): string | null {
  const s = equalWeight?.strategy
  const u = equalWeight?.benchmark
  if (s?.annual_return_pct === undefined || u?.annual_return_pct === undefined) return null
  const verb = s.annual_return_pct < u.annual_return_pct ? 'returned more' : 'did not return more'
  const rv =
    s.return_per_volatility !== undefined && s.return_per_volatility === u.return_per_volatility
      ? `, with the same return per unit of volatility (${fmtNum(s.return_per_volatility)})`
      : ''
  const basis = equalWeight?.reproducible
    ? 'reproducible from its kept script'
    : 'not reproducible: a historical diagnostic'
  return `Supporting diagnostic, not decisive (${basis}): the equal-weight eligible universe ${verb} (${fmtNum(u.annual_return_pct, 1)}% against ${fmtNum(s.annual_return_pct, 1)}% a year${rv}).`
}

function limitations(
  rows: EffectiveRow[],
  portfolio: PortfolioView | null,
  run: RunRecord,
  diagnostics: CitedDiagnostic[]
): string[] {
  const out: string[] = []
  const hardOpen = rows.filter((r) => r.hard && r.status === 'not_evaluated').map((r) => r.gate)
  if (hardOpen.length > 0) {
    out.push(
      `Hard gates not measured for this run: ${hardOpen.join(', ')}. Not measured is not a pass.`
    )
  }
  const survivorship = rows.find((r) => r.gate === 'G4.7')
  if (survivorship && /survivor/i.test(survivorship.evidence)) {
    out.push(
      "The universe is today's F&O list applied to the past, so names that later left are missing (survivorship bias)."
    )
  }
  if (run.costs?.model === 'flat-round-trip-bps') {
    out.push(
      `The headline uses the engine's flat ${fmtNum(run.costs.round_trip_bps, 0)} bps round-trip cost; this run predates the statutory headline cost.`
    )
  }
  if (portfolio?.source === 'reconstructed') {
    const when = portfolio.evaluated_ist ? ` (${portfolio.evaluated_ist.slice(0, 10)})` : ''
    out.push(
      `Portfolio figures were rebuilt after the run from its frozen trades and published closes${when}; the run did not record them.`
    )
  }
  for (const d of diagnostics) {
    if (d.reproducible === false) {
      out.push(
        `${d.title ?? d.name} is a historical diagnostic that cannot be reproduced; it is not used as decisive evidence.`
      )
    }
  }
  return out
}

export interface RunSummaryModel {
  verdict: RunVerdictValue | null
  researchState: ResearchState | null
  rejectionBasis: string | null
  promotion: PromotionDecision | null
  /** One line that reconciles the three answers when they look contradictory. */
  reconcile: string | null
  portfolio: PortfolioView | null
  randomEntry: RandomEntryComparison | null
  equalWeight: CitedDiagnostic | null
  selectionNote: string | null
  /** The equal-weight comparison, worded as a supporting diagnostic. */
  supportingNote: string | null
  strengths: SummaryFinding[]
  failed: SummaryFinding[]
  corrected: GateCorrection[]
  limitations: string[]
  action: RecommendedAction
}

export function buildRunSummary(
  idea: SummaryIdea,
  run: RunRecord,
  detail: RunDetail
): RunSummaryModel {
  const evaluations = detail.evaluations ?? { corrections: [], portfolio: null, diagnostics: [] }
  const rows = effectiveRows(run.evaluation, evaluations.corrections)
  const portfolio = evaluations.portfolio
  const random = randomEntry(run)
  const equalWeight = evaluations.diagnostics.find(
    (d) => d.name === 'equal_weight_eligible_universe'
  )
  const verdict = detail.verdict?.run_verdict ?? run.run_verdict ?? null
  const promotion = detail.promotion?.stages?.backtest?.decision ?? run.promotion_decision ?? null
  const state = idea.research_state ?? null
  const reconcile =
    verdict && verdict !== 'NO_EDGE' && state && STOPPED_STATES.includes(state)
      ? 'These answer different questions. The gates alone did not rule the strategy out, so the calculation is not a failure; the researcher stopped it on the full evidence, including benchmarks that are not gates. Promotion stays not ready either way.'
      : null
  return {
    verdict,
    researchState: state,
    rejectionBasis: idea.rejection_basis ?? null,
    promotion,
    reconcile,
    portfolio,
    randomEntry: random,
    equalWeight: equalWeight ?? null,
    selectionNote: selectionNote(
      run,
      portfolio,
      random,
      rows.some((r) => r.gate === 'G3.2' && r.status === 'fail')
    ),
    supportingNote: supportingNote(equalWeight),
    strengths: rows
      .filter((r) => r.status === 'pass' && (r.hard || DECISIVE_PASSES.has(r.gate)))
      .map(finding),
    failed: rows.filter((r) => r.status === 'fail').map(finding),
    corrected: evaluations.corrections,
    limitations: limitations(rows, portfolio, run, evaluations.diagnostics),
    action: recommendedAction(idea, detail),
  }
}
