/**
 * A plain-language reading of one recorded run: validity, what passed, what fell short, what is
 * not yet established, the limitations and evidence-linked recommendations.
 *
 * Presentation only. Every statement comes from the immutable run record (evaluation rows,
 * validity, data quality, the derived verdict and promotion) or the idea's tuning records. Nothing
 * here derives a verdict, scores a strategy or changes what a gate concluded.
 */
import type { GateRow, Promotion, RunDetail, RunRecord, TuningSummary } from '@/api/backtestResults'
import { fmtNum, isExtrapolated, THIN_SAMPLE_TRADES } from '@/lib/backtestResults'

export interface GateFinding {
  gate: string
  title: string
  observed: string
  threshold: string
  hard: boolean
}

export interface Recommendation {
  text: string
  /** The recorded evidence behind it, e.g. "G1.3: observed -0.120, needs >= 0". */
  evidence: string
}

export type ValidityState = 'valid' | 'invalid' | 'not_recorded'

export interface RunAssessment {
  validity: { state: ValidityState; checks: number; failures: string[] }
  worked: GateFinding[]
  shortfalls: GateFinding[]
  /** Not evaluated, yet required at the backtest stage: missing evidence, not a failure. */
  missing: GateFinding[]
  /** Not evaluated and required only at the paper or live stage. */
  later: number
  notApplicable: number
  limitations: string[]
  /**
   * The capabilities this run's record listed as not yet verified when it was made. A fact about
   * that run, frozen with it; not the engine's current state, which only the readiness ledger holds.
   */
  recordedUnverified: string[]
  recommendations: Recommendation[]
}

export function gateValueText(value: GateRow['value']): string {
  if (value === null || value === undefined) return 'n/a'
  return typeof value === 'number' ? fmtNum(value, 3) : String(value)
}

function finding(row: GateRow): GateFinding {
  return {
    gate: row.gate,
    title: row.title,
    observed: gateValueText(row.value),
    threshold: row.threshold,
    hard: row.hard,
  }
}

function evidenceText(f: GateFinding): string {
  return `${f.gate} ${f.title}: observed ${f.observed}, needs ${f.threshold || 'n/a'}`
}

/** Gates the promotion evaluator checks at the backtest stage, or null when it did not run. */
function backtestStageGates(promotion: Promotion | null): Set<string> | null {
  const gates = promotion?.stages?.backtest?.gates
  return gates ? new Set(gates.map((g) => g.gate)) : null
}

function inStage(gate: string, stage: Set<string> | null): boolean {
  if (!stage) return true
  if (stage.has(gate)) return true
  // The stage may list a group (G2) while the evaluation rows are G2.1, G2.2.
  return [...stage].some((g) => gate.startsWith(`${g}.`))
}

/** One recommendation per family, from a failing or missing gate. Order is the gate order. */
const FAMILY_ADVICE: [RegExp, string][] = [
  [
    /^G1\.6$|^G6\.5$/,
    'Review stop behaviour: stops that sit too close or gap through change what a trade can lose.',
  ],
  [
    /^G1\./,
    'Investigate cost sensitivity: costs decide whether a small gross edge survives real trading.',
  ],
  [
    /^G2\.(1|7)$/,
    'Collect more observations: the sample is too small to tell an edge from chance. More data, not more tuning.',
  ],
  [
    /^G2\./,
    'Treat the edge as unproven: it is not statistically distinguishable from noise after the trials tried.',
  ],
  [
    /^G3\./,
    'Examine the out-of-sample result: performance on unseen data decides whether the in-sample result was real. Do not re-tune on this hold-out.',
  ],
  [
    /^G4\.1$/,
    'Test parameter stability: check whether nearby settings give similar results before trusting these values.',
  ],
  [
    /^G4\./,
    'Check robustness: the result depends on conditions (period, symbols, direction or timing) that may not hold.',
  ],
  [
    /^G5\./,
    'Review risk constraints: drawdown, sizing or position limits are not met as modelled.',
  ],
  [
    /^G6\./,
    'Review the execution model: the backtest does not yet trade the way the exchange and broker would.',
  ],
]

function familyOf(gate: string): number {
  return FAMILY_ADVICE.findIndex(([re]) => re.test(gate))
}

function recommendations(
  shortfalls: GateFinding[],
  missing: GateFinding[],
  tuning: TuningSummary[]
): Recommendation[] {
  const out: Recommendation[] = []
  const seen = new Set<number>()
  for (const f of [...shortfalls, ...missing]) {
    const family = familyOf(f.gate)
    if (family < 0 || seen.has(family)) continue
    seen.add(family)
    let text = FAMILY_ADVICE[family][1]
    if (f.gate === 'G4.1' && tuning.length > 0) {
      text =
        'Parameter stability: a tuning record exists; compare its neighbourhood result below with this run.'
    }
    const status = shortfalls.includes(f) ? 'failed' : 'not established'
    out.push({ text, evidence: `${evidenceText(f)} (${status})` })
  }
  return out.slice(0, 4)
}

function limitations(run: RunRecord, detail: RunDetail): string[] {
  const out = [...(detail.verdict?.qualifications ?? [])]
  for (const item of detail.data_quality?.items ?? []) {
    if (item.severity === 'warning') out.push(`Data quality: ${item.detail}`)
  }
  const trades = run.metrics?.trades
  if (trades !== undefined && trades > 0 && trades < THIN_SAMPLE_TRADES) {
    out.push(`Thin sample: ${trades} trades, fewer than ${THIN_SAMPLE_TRADES}.`)
  }
  if (run.metrics && isExtrapolated(run.metrics, run.years)) {
    const span =
      typeof run.years === 'number'
        ? `${run.years} years tested`
        : `${run.metrics.days} trading days`
    out.push(`Short history: ${span}, so annualised ratios are extrapolated.`)
  }
  return out
}

function validity(detail: RunDetail): RunAssessment['validity'] {
  const v = detail.validity
  if (!v) return { state: 'not_recorded', checks: 0, failures: [] }
  return {
    state: v.ok ? 'valid' : 'invalid',
    checks: v.checks_run?.length ?? 0,
    failures: (v.failures ?? []).map((f) => `${f.check}: ${f.detail}`),
  }
}

export function assessRun(
  run: RunRecord,
  detail: RunDetail,
  tuning: TuningSummary[] = []
): RunAssessment {
  const rows = (run.evaluation?.dimensions ?? []).flatMap((d) => d.rows ?? [])
  const stage = backtestStageGates(detail.promotion)
  const worked = rows
    .filter((r) => r.status === 'pass')
    .map(finding)
    .sort((a, b) => Number(b.hard) - Number(a.hard))
  const shortfalls = rows.filter((r) => r.status === 'fail').map(finding)
  const pending = rows.filter((r) => r.status === 'not_evaluated')
  // Hard gates first, then gate order (Array.prototype.sort is stable).
  const missing = pending
    .filter((r) => inStage(r.gate, stage))
    .map(finding)
    .sort((a, b) => Number(b.hard) - Number(a.hard))
  return {
    validity: validity(detail),
    worked,
    shortfalls,
    missing,
    later: pending.length - missing.length,
    notApplicable: rows.filter((r) => r.status === 'not_applicable').length,
    limitations: limitations(run, detail),
    recordedUnverified: [...(detail.missing_capabilities ?? [])],
    recommendations: recommendations(shortfalls, missing, tuning),
  }
}

export type Stability = 'stable' | 'fragile' | 'unknown'

/**
 * Whether the selected setting held up across its neighbours in-sample: every usable neighbour
 * positive is "stable", any neighbour at or below zero is "fragile". Read from the record only.
 */
export function tuningStability(t: TuningSummary): Stability {
  const worst = t.selected?.region_worst_net_r
  if (worst === null || worst === undefined || !Number.isFinite(worst)) return 'unknown'
  if ((t.selected?.region_size ?? 0) < 2) return 'unknown'
  return worst > 0 ? 'stable' : 'fragile'
}

/** True when the run's parameters include every selected value of the tuning record. */
export function runUsesSelection(run: RunRecord, t: TuningSummary): boolean | null {
  const values = t.selected?.values
  if (!values || !run.params) return null
  return Object.entries(values).every(([k, v]) => run.params?.[k] === v)
}
