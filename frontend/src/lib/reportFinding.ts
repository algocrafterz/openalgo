import type { BugKind, BugSeverity } from '@/api/bugs'
import type { Finding } from '@/lib/workflowFindings'

/**
 * Defaults for the "Report as bug" form. They are suggestions the trader can change, and none of
 * them claims an impact: severity starts at Medium and must be confirmed by the trader, and the
 * impact stays "Not assessed" until the trader writes something else.
 */

export const AREAS = [
  'ops',
  'signal_engine',
  'openalgo-core',
  'sandbox',
  'broker',
  'data',
  'strategy',
] as const
export type Area = (typeof AREAS)[number]

export interface ReportDefaults {
  severity: BugSeverity
  kind: BugKind
  area: Area
  impact: string
}

/** What the tracker stores as the trading impact until someone describes it. */
export const NOT_ASSESSED = 'Not assessed'

export function suggestedKind(category: Finding['category']): BugKind {
  if (category === 'orders' || category === 'broker') return 'EXECUTION'
  if (category === 'data' || category === 'positions') return 'DATA'
  return 'BUG'
}

export function suggestedArea(category: Finding['category']): Area {
  if (category === 'broker') return 'broker'
  if (category === 'data') return 'data'
  if (category === 'strategy') return 'strategy'
  return 'ops'
}

export function defaultsFor(finding: Finding): ReportDefaults {
  return {
    severity: 'MEDIUM',
    kind: suggestedKind(finding.category),
    area: suggestedArea(finding.category),
    impact: NOT_ASSESSED,
  }
}

export interface ReportPayload {
  day: string
  phase: string
  group: string
  name: string
  severity: BugSeverity
  kind: BugKind
  area: Area
  impact: string
  /** The trader has confirmed the risk level above. The server refuses a report without it. */
  severity_confirmed: boolean
}

/** Only the reference to the finding and the trader's choices leave the browser, never the evidence. */
export function buildPayload(
  finding: Finding,
  day: string,
  form: ReportDefaults,
  severityConfirmed: boolean
): ReportPayload {
  return {
    day,
    phase: finding.phase,
    group: finding.group,
    name: finding.name,
    severity: form.severity,
    kind: form.kind,
    area: form.area,
    impact: form.impact.trim() || NOT_ASSESSED,
    severity_confirmed: severityConfirmed,
  }
}

export function findingRef(f: { phase: string; group: string; name: string }): string {
  return `${f.phase}|${f.group}|${f.name}`
}
