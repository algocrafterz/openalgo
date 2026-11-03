/**
 * Pure helpers for the AI evaluation panel on the Performance page.
 *
 * Nothing here calls the server or a model. The panel builds a package on
 * request, the trader copies a prompt and a file by hand, and pastes the answer
 * back. These helpers decide what the panel shows and when a package is stale.
 */

import type {
  ConfidenceLevel,
  DateRange,
  EvaluationPackage,
  EvaluationProblem,
  EvidenceLabel,
  FindingKind,
  ObservationKind,
  PerformancePeriod,
} from '@/api/strategyPnl'
import type { CapitalMode } from '@/lib/capital'
import type { StrategyFilter } from '@/lib/strategyFilter'

/** The only verdict words, weakest evidence first. */
export const EVIDENCE_LABELS: readonly EvidenceLabel[] = [
  'insufficient data',
  'no distinguishable edge',
  'possible edge (screening only)',
  'likely negative',
]

/** Badge colours per evidence label. Neutral unless the label itself is negative. */
export const EVIDENCE_LABEL_CLASS: Record<EvidenceLabel, string> = {
  'insufficient data': 'border-border bg-muted text-muted-foreground',
  'no distinguishable edge': 'border-border bg-secondary text-secondary-foreground',
  'possible edge (screening only)':
    'border-sky-500/40 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  'likely negative': 'border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300',
}

/** Shown for a label the page does not know; the text is never trusted as a class. */
export const UNKNOWN_LABEL_CLASS = 'border-border bg-muted text-muted-foreground'

export function evidenceLabelClass(label: string | null | undefined): string {
  return (EVIDENCE_LABELS as readonly string[]).includes(label ?? '')
    ? EVIDENCE_LABEL_CLASS[label as EvidenceLabel]
    : UNKNOWN_LABEL_CLASS
}

export interface KindBadge {
  text: string
  className: string
}

/** Finding kinds. A rule of thumb is neutral; it is never coloured like a verdict. */
export const FINDING_KIND_BADGE: Record<FindingKind, KindBadge> = {
  math: { text: 'Calculated', className: 'border-border bg-secondary text-secondary-foreground' },
  heuristic: { text: 'Rule of thumb', className: 'border-border bg-background text-foreground' },
  not_available: {
    text: 'Not available',
    className: 'border-dashed border-border bg-muted text-muted-foreground',
  },
}

export function findingKindBadge(kind: string): KindBadge {
  return FINDING_KIND_BADGE[kind as FindingKind] ?? FINDING_KIND_BADGE.not_available
}

/** Claude's own statement kinds, in trader words. */
export const OBSERVATION_KIND_TEXT: Record<ObservationKind, string> = {
  observed: 'Observed',
  inference: 'Inference',
  hypothesis: 'Hypothesis to test',
}

export function observationKindText(kind: string): string {
  return OBSERVATION_KIND_TEXT[kind as ObservationKind] ?? 'Statement'
}

export const CONFIDENCE_TEXT: Record<ConfidenceLevel, string> = {
  low: 'low',
  medium: 'medium',
  high: 'high',
}

/**
 * One string that names everything a package depends on. Two selections give the
 * same signature only when the server would build the same data. The include and
 * exclude lists are sorted, so the order the trader clicked in does not matter.
 */
export function selectionSignature(
  filter: StrategyFilter,
  period: PerformancePeriod,
  range: DateRange | undefined,
  capital: number | null,
  mode: CapitalMode
): string {
  return JSON.stringify({
    include: [...filter.include].sort(),
    exclude: [...filter.exclude].sort(),
    none: filter.none === true,
    period,
    range: period === 'custom' && range ? [range.start, range.end] : null,
    capital,
    mode,
  })
}

/** The file name the server chose, with anything that is not a safe file character replaced. */
export function downloadFilename(pkg: Pick<EvaluationPackage, 'export'>): string {
  const raw = pkg.export?.data_filename
  const cleaned = typeof raw === 'string' ? raw.replace(/[^A-Za-z0-9._-]+/g, '_') : ''
  return cleaned && cleaned !== '.' && cleaned !== '..' ? cleaned : 'performance-evaluation.json'
}

export interface PromptChoice {
  text: string
  /** True when the data is inside the text. False means the trader attaches the file. */
  includesData: boolean
  /** A plain sentence for the trader when the choice differs from what they asked for. */
  note: string | null
}

export const ATTACH_NOTE = 'Attach the downloaded file to your message in Claude.'
export const TOO_LARGE_NOTE =
  'The data is too large to paste into the prompt. Attach the downloaded file instead.'

/** The prompt to copy. Falls back to the prompt without data when none with data exists. */
export function promptForUse(
  pkg: Pick<EvaluationPackage, 'prompt'>,
  withData: boolean
): PromptChoice {
  const withDataText = pkg.prompt.text_with_data
  if (withData && withDataText) {
    return { text: withDataText, includesData: true, note: null }
  }
  if (withData) {
    return { text: pkg.prompt.text, includesData: false, note: TOO_LARGE_NOTE }
  }
  return { text: pkg.prompt.text, includesData: false, note: ATTACH_NOTE }
}

export const MAX_PROBLEM_LINES = 20

/** Problems from a failed check as plain lines, at most 20. */
export function problemsToLines(problems: EvaluationProblem[] | undefined): string[] {
  if (!Array.isArray(problems)) return []
  return problems.slice(0, MAX_PROBLEM_LINES).map((problem) => {
    const path = typeof problem?.path === 'string' ? problem.path : ''
    const message = typeof problem?.message === 'string' ? problem.message : ''
    return path ? `${path}: ${message}` : message
  })
}

/** Rule parameters as short "name = value" parts, for the small text under a rule of thumb. */
export function ruleParameterParts(parameters: Record<string, unknown> | undefined): string[] {
  if (!parameters || typeof parameters !== 'object') return []
  return Object.entries(parameters).map(([key, value]) => `${key} = ${String(value)}`)
}

export const MAX_ANALYSIS_CHARS = 60_000
