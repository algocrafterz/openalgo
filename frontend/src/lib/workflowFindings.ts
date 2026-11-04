import type { WorkflowCheck, WorkflowDay, WorkflowStep } from '@/api/opsWorkflow'
import type { ExecutionMetric } from '@/lib/reportParsers'

/**
 * Turns the day's recorded checks into findings a trader can scan: a severity, an operational
 * category and the checkpoint it came from. Nothing here is measured. Severity follows the status
 * the check itself reported, and the category is a fixed lookup (edit CATEGORY_RULES to maintain it).
 */

export type PhaseKey = 'open-day' | 'liveness' | 'close-day'
export type Severity = 'critical' | 'warning' | 'unverified' | 'info'
export type Health = 'healthy' | 'degraded' | 'critical' | 'nodata'

export const PHASE_CHECKS: Record<PhaseKey, string[]> = {
  'open-day': ['open-day'],
  liveness: ['liveness'],
  'close-day': ['logs', 'value-areas', 'jobs'],
}

export const CATEGORIES = [
  { key: 'broker', label: 'Broker connectivity and API health' },
  { key: 'orders', label: 'Order execution and order lifecycle' },
  { key: 'positions', label: 'Position and trade reconciliation' },
  { key: 'risk', label: 'Risk management and exposure' },
  { key: 'strategy', label: 'Strategy execution and runtime health' },
  { key: 'data', label: 'Market data freshness and integrity' },
  { key: 'jobs', label: 'Scheduled jobs and background services' },
  { key: 'infra', label: 'Database and infrastructure health' },
  { key: 'other', label: 'Other' },
] as const

export type CategoryKey = (typeof CATEGORIES)[number]['key']

/** First matching rule wins. Matched against a check name or an execution-health metric name. */
const CATEGORY_RULES: [RegExp, CategoryKey][] = [
  [/^task |crontab|^phase |windows task|close-day \d{4}/i, 'jobs'],
  [/risk counters|risk/i, 'risk'],
  [/exit uncertain|order rejections|place_order|timeout sending order|signal-to-ack/i, 'orders'],
  [/positionbook|reconcil/i, 'positions'],
  [/trades\.db/i, 'infra'],
  [/broker session|funds api|trading mode|429|limiter|data calls|dns|name resolution/i, 'broker'],
  [/stale|coverage|dataset|rows added|websocket|zeromq|history_service|outage/i, 'data'],
  [
    /breakingtrade|engine startup|runner cycle|signals reaching|retry loops|pipeline cycles/i,
    'strategy',
  ],
  [/services|scheduler/i, 'jobs'],
  [/openalgo health|disk|clock|ntp/i, 'infra'],
]

export function categoryOf(name: string): CategoryKey {
  return CATEGORY_RULES.find(([re]) => re.test(name))?.[1] ?? 'other'
}

export const SEVERITY_LABEL: Record<Severity, string> = {
  critical: 'Critical',
  warning: 'Warning',
  unverified: 'Unverified',
  info: 'Informational',
}

const SEVERITY_ORDER: Record<Severity, number> = { critical: 0, warning: 1, unverified: 2, info: 3 }

export const ACTION_TEXT: Record<Severity, string> = {
  critical: 'Action required',
  warning: 'Review',
  unverified: 'Check by hand',
  info: 'No action',
}

export interface Finding {
  id: string
  severity: Severity
  category: CategoryKey
  /** What happened, in plain words when the check provided them. */
  title: string
  /** The raw evidence line, shown only when expanded. */
  evidence: string
  phase: PhaseKey
  /** With `name`, what the server uses to find this finding again in the day's stored results. */
  group: string
  name: string
  /** Time the checkpoint ran (HH:MM:SS), when its status file has one. */
  checkedAt?: string
  /** Report that holds the detail, if one exists for this finding. */
  report?: 'execution' | 'errors' | 'jobs' | 'value' | 'open' | 'after'
  source: string
}

export function severityOfCheck(status: WorkflowCheck['status']): Severity {
  if (status === 'FAIL') return 'critical'
  if (status === 'WARN') return 'warning'
  if (status === 'NOT VERIFIED') return 'unverified'
  return 'info'
}

const CHECK_REPORT: Record<string, Finding['report']> = {
  'open-day': 'open',
  liveness: 'after',
  logs: 'errors',
  'value-areas': 'value',
  jobs: 'jobs',
}

const CHECK_SOURCE: Record<string, string> = {
  'open-day': 'Readiness check',
  liveness: 'Liveness check',
  logs: 'Error review',
  'value-areas': 'Value areas',
  jobs: 'Scheduled jobs check',
}

function startOf(day: WorkflowDay, phase: PhaseKey): string | undefined {
  return day.phases.find((p) => p.phase === phase)?.steps?.[0]?.started
}

/** Every check that is not plainly OK, plus every step that exited with an error. */
export function buildFindings(day: WorkflowDay): Finding[] {
  const out: Finding[] = []
  for (const phase of Object.keys(PHASE_CHECKS) as PhaseKey[]) {
    const checkedAt = startOf(day, phase)
    for (const key of PHASE_CHECKS[phase]) {
      for (const c of day.checks[key]?.checks ?? []) {
        const severity = severityOfCheck(c.status)
        if (severity === 'info') continue
        out.push({
          id: `${phase}:${key}:${c.name}`,
          group: key,
          name: c.name,
          severity,
          category: categoryOf(c.name),
          title: c.plain || c.name,
          evidence: `${c.name}: ${c.evidence}`,
          phase,
          checkedAt,
          report: CHECK_REPORT[key],
          source: CHECK_SOURCE[key] ?? key,
        })
      }
    }
    for (const s of day.phases.find((p) => p.phase === phase)?.steps ?? []) {
      if (!stepFailed(day, phase, s)) continue
      out.push({
        id: `${phase}:step:${s.step}`,
        group: 'step',
        name: s.step,
        severity: 'critical',
        category: 'jobs',
        title:
          s.exit === 124
            ? `The "${s.step}" step stopped at its deadline.`
            : `The "${s.step}" step failed.`,
        evidence: `step ${s.step} exited with ${s.exit} at ${s.started}`,
        phase,
        checkedAt: s.started,
        source: 'Workflow step',
      })
    }
  }
  return out
}

const METRIC_SEVERITY: Record<string, Severity> = { REGRESSION: 'warning', WATCH: 'info' }

/** Execution-health rows the report itself marks WATCH or REGRESSION. */
export function metricFindings(rows: ExecutionMetric[], day: WorkflowDay): Finding[] {
  const checkedAt = startOf(day, 'close-day')
  return rows
    .filter((r) => r.status in METRIC_SEVERITY)
    .map((r) => ({
      id: `close-day:execution:${r.metric}`,
      group: 'execution',
      name: r.metric,
      severity: /exit uncertain/i.test(r.metric) ? 'critical' : METRIC_SEVERITY[r.status],
      category: categoryOf(r.metric),
      title: `${r.metric}: ${r.today} (${r.status.toLowerCase()})`,
      evidence: `${r.metric}: today ${r.today}${r.prev !== undefined ? `, previous day ${r.prev}` : ''}`,
      phase: 'close-day' as const,
      checkedAt,
      report: 'execution' as const,
      source: 'Execution health',
    }))
}

export function sortFindings(findings: Finding[]): Finding[] {
  return [...findings].sort(
    (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity] || a.id.localeCompare(b.id)
  )
}

export function countSeverities(findings: Finding[]): Record<Severity, number> {
  const counts: Record<Severity, number> = { critical: 0, warning: 0, unverified: 0, info: 0 }
  for (const f of findings) counts[f.severity] += 1
  return counts
}

/** The check result a step writes, by step name. `check` is open-day or liveness, by phase. */
function resultKey(phase: PhaseKey, step: string): string | null {
  if (step === 'check') return phase === 'open-day' || phase === 'liveness' ? phase : null
  if (step === 'logs' || step === 'jobs') return step
  if (step === 'value_areas') return 'value-areas'
  return null
}

/**
 * True when a step really failed. The ops check commands exit 1 by design when their own verdict is
 * RED (signal_engine/ops/__main__.py). Such a step ran and wrote its report, so the RED result is
 * shown as findings, not a second time as a broken step. Any other exit code, or an exit 1 with no
 * RED result on record, is a real failure.
 */
export function stepFailed(day: WorkflowDay, phase: PhaseKey, step: WorkflowStep): boolean {
  if (step.exit === 0) return false
  const key = resultKey(phase, step.step)
  const reportedRed = step.exit === 1 && key !== null && day.checks[key]?.verdict === 'RED'
  return !reportedRed
}

/**
 * The checkpoint a trader should land on: the one holding the most severe finding, then the most
 * findings of that severity, then the earliest. Null when no checkpoint has a finding to act on.
 */
export function defaultPhase(findings: Finding[]): PhaseKey | null {
  const phases = Object.keys(PHASE_CHECKS) as PhaseKey[]
  let best: { phase: PhaseKey; rank: number; count: number } | null = null
  for (const phase of phases) {
    const mine = findings.filter((f) => f.phase === phase && f.severity !== 'info')
    if (mine.length === 0) continue
    const rank = Math.min(...mine.map((f) => SEVERITY_ORDER[f.severity]))
    const count = mine.filter((f) => SEVERITY_ORDER[f.severity] === rank).length
    if (!best || rank < best.rank || (rank === best.rank && count > best.count)) {
      best = { phase, rank, count }
    }
  }
  return best?.phase ?? null
}

/** A checkpoint that wrote a status file and had no failing step. This says nothing about health. */
export function phaseCompleted(day: WorkflowDay, phase: PhaseKey): boolean | null {
  const steps = day.phases.find((p) => p.phase === phase)?.steps
  if (!steps) return null
  return steps.every((s) => !stepFailed(day, phase, s))
}

/**
 * Health is judged from what was found, not from whether the checkpoints ran.
 * Unverified findings keep a day out of Healthy. With no checkpoint recorded there is nothing to judge.
 */
export function healthOf(day: WorkflowDay, findings: Finding[]): Health {
  if (day.phases.every((p) => p.steps === null) && !day.verdict) return 'nodata'
  const counts = countSeverities(findings)
  if (counts.critical > 0 || day.verdict?.verdict === 'RED') return 'critical'
  if (
    counts.warning > 0 ||
    counts.unverified > 0 ||
    day.verdict?.verdict === 'AMBER' ||
    Object.values(day.checks).some((c) => c.verdict !== 'GREEN')
  ) {
    return 'degraded'
  }
  return 'healthy'
}
