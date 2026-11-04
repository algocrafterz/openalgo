import type { Bug, BugStatus } from '@/api/bugs'

/**
 * Bug status groups, trading categories, lifecycle and summary counts, shared by the Bug Tracker
 * and the Daily Workflow so both pages count the same way. The persisted status is always
 * authoritative; nothing here changes it or invents a lifecycle event.
 */

export type BugGroup = 'todo' | 'proving' | 'done'

export const GROUP_STATUSES: Record<BugGroup, BugStatus[]> = {
  todo: ['open', 'in_progress'],
  proving: ['fixed'],
  done: ['verified', 'wontfix'],
}

export function groupOfStatus(status: BugStatus): BugGroup {
  const found = (Object.keys(GROUP_STATUSES) as BugGroup[]).find((g) =>
    GROUP_STATUSES[g].includes(status)
  )
  return found ?? 'todo'
}

export function groupOf(bug: Bug): BugGroup {
  return groupOfStatus(bug.status)
}

/** The server writes this into `sources` for a bug reported from a Daily Workflow finding. */
export const FINDING_MARKER = 'workflow-finding:'

export function reportedFromWorkflow(bug: Bug): boolean {
  return bug.sources.some((s) => s.startsWith(FINDING_MARKER))
}

/** Sources a reader can open: the marker is bookkeeping, not a reference. */
export function readableSources(bug: Bug): string[] {
  return bug.sources.filter((s) => !s.startsWith(FINDING_MARKER))
}

/**
 * High risk is the tracker's severity (HIGH). It is not a statement that live trading was affected:
 * the tracker has no independent impact field, so nothing here is called "trading critical".
 */
export function isHighRisk(bug: Bug): boolean {
  return bug.severity === 'HIGH'
}

export interface BugSummary {
  needsFix: number
  highNeedsFix: number
  awaitingProof: number
  highAwaitingProof: number
  verified: number
  closedNotFixing: number
}

export function summarizeBugs(bugs: Bug[]): BugSummary {
  const out: BugSummary = {
    needsFix: 0,
    highNeedsFix: 0,
    awaitingProof: 0,
    highAwaitingProof: 0,
    verified: 0,
    closedNotFixing: 0,
  }
  for (const b of bugs) {
    const high = isHighRisk(b)
    if (groupOf(b) === 'todo') {
      out.needsFix += 1
      if (high) out.highNeedsFix += 1
    } else if (groupOf(b) === 'proving') {
      out.awaitingProof += 1
      if (high) out.highAwaitingProof += 1
    } else if (b.status === 'verified') out.verified += 1
    else out.closedNotFixing += 1
  }
  return out
}

export const BUG_CATEGORIES = [
  { key: 'execution', label: 'Execution' },
  { key: 'risk', label: 'Risk Management' },
  { key: 'data', label: 'Data Integrity' },
  { key: 'strategy', label: 'Strategy Logic' },
  { key: 'broker', label: 'Broker Integration' },
  { key: 'infra', label: 'Infrastructure' },
  { key: 'reporting', label: 'Reporting' },
  { key: 'other', label: 'Other' },
] as const

export type BugCategoryKey = (typeof BUG_CATEGORIES)[number]['key']

export function categoryLabel(key: BugCategoryKey): string {
  return BUG_CATEGORIES.find((c) => c.key === key)?.label ?? 'Other'
}

/**
 * Category rules. Deterministic, and documented here because the tracker has no category field.
 *
 * Precedence, first step that decides wins:
 *   1. Component: `broker` is Broker Integration, `ops` is Infrastructure.
 *   2. Title words. Each rule below that matches the title adds its category. Exactly one distinct
 *      category decides. Two or more is ambiguous and the bug is Other, never a guess.
 *   3. Kind: DATA is Data Integrity, EXECUTION is Execution, TUNING-IDEA is Strategy Logic.
 *   4. Anything left is Other. Component alone (signal_engine, openalgo-core, sandbox) says where
 *      the code lives, not what failed, so it is not used.
 * Edit TITLE_RULES to maintain it; keep each pattern narrow.
 */
const TITLE_RULES: [RegExp, BugCategoryKey][] = [
  [/stop[- ]?loss|trailing|square[- ]?off|exposure|risk limit|\bcap\b|\bstop\b/i, 'risk'],
  [/\breports?\b|\bsection\b|\bwording\b|\bdisplay|eod\.sh/i, 'reporting'],
  [/\bexit\b|\bfills?\b|\borders?\b|timed-out|time out|timeout|reconcil/i, 'execution'],
  [/value[- ]area|\bduplicate\b|\bphantom\b|stale|missing data/i, 'data'],
  [/\bsizing\b|\bstrategy logic\b/i, 'strategy'],
]

const KIND_CATEGORY: Partial<Record<Bug['kind'], BugCategoryKey>> = {
  DATA: 'data',
  EXECUTION: 'execution',
  'TUNING-IDEA': 'strategy',
}

export function categoryOfBug(bug: Bug): BugCategoryKey {
  if (bug.area === 'broker') return 'broker'
  if (bug.area === 'ops') return 'infra'
  const fromTitle = new Set(TITLE_RULES.filter(([re]) => re.test(bug.title)).map(([, c]) => c))
  if (fromTitle.size === 1) return [...fromTitle][0]
  if (fromTitle.size > 1) return 'other'
  return KIND_CATEGORY[bug.kind] ?? 'other'
}

/** The "how we will know" event noted by /resolve verify ("... Proves it: <event>"). */
export function acceptanceCriteria(bug: Bug): string | null {
  for (const entry of [...bug.history].reverse()) {
    const at = entry.note.indexOf('Proves it:')
    if (at >= 0) return entry.note.slice(at + 'Proves it:'.length).trim()
  }
  return null
}

export type StepKey = 'reported' | 'confirmed' | 'fixed' | 'awaiting' | 'verified'
/** done: reached, with record. current: where it is now. pending: not reached. unrecorded: passed but no history entry says so. */
export type StepState = 'done' | 'current' | 'pending' | 'unrecorded'

export interface LifecycleStep {
  key: StepKey
  label: string
  state: StepState
  /** YYYY-MM-DD, only when a history entry records it. */
  date?: string
}

const day = (at: string | undefined) => (at ? at.slice(0, 10) : undefined)

/** When the bug entered its current run of `status`: the first entry of the last contiguous run. */
function enteredAt(bug: Bug, status: BugStatus): string | undefined {
  let i = bug.history.length - 1
  while (i >= 0 && bug.history[i].status !== status) i -= 1
  if (i < 0) return undefined
  while (i > 0 && bug.history[i - 1].status === status) i -= 1
  return bug.history[i].at
}

const hasEntry = (bug: Bug, status: BugStatus) => bug.history.some((h) => h.status === status)

/** True when a fixed or verified entry exists and the bug is back in open or in progress. */
export function wasReopened(bug: Bug): boolean {
  return (
    GROUP_STATUSES.todo.includes(bug.status) &&
    bug.history.some((h) => h.status === 'fixed' || h.status === 'verified')
  )
}

/** The five-step view of the persisted status. wontfix has no lifecycle (it is a separate outcome). */
export function lifecycle(bug: Bug): LifecycleStep[] | null {
  if (bug.status === 'wontfix') return null
  const s = bug.status
  const fixedRecorded = hasEntry(bug, 'fixed')
  const first = bug.history[0]
  const past = (reached: boolean, recorded: boolean): StepState =>
    reached ? (recorded ? 'done' : 'unrecorded') : 'pending'

  const steps: LifecycleStep[] = [
    {
      key: 'reported',
      label: 'Reported',
      state: first ? 'done' : 'unrecorded',
      date: day(first?.at) ?? bug.first_seen,
    },
    {
      key: 'confirmed',
      label: s === 'in_progress' ? 'Confirmed, in progress' : 'Confirmed',
      state: s === 'open' || s === 'in_progress' ? 'current' : 'done',
    },
    {
      key: 'fixed',
      label: 'Fixed',
      state: past(s === 'fixed' || s === 'verified', fixedRecorded),
      date: s === 'fixed' || s === 'verified' ? day(enteredAt(bug, 'fixed')) : undefined,
    },
    {
      key: 'awaiting',
      label: 'Awaiting proof',
      state: s === 'fixed' ? 'current' : s === 'verified' ? past(true, fixedRecorded) : 'pending',
    },
    {
      key: 'verified',
      label: 'Verified',
      state: s === 'verified' ? 'current' : 'pending',
      date: s === 'verified' ? day(enteredAt(bug, 'verified')) : undefined,
    },
  ]
  return steps
}

export function lastUpdated(bug: Bug): string {
  return bug.updated_at || bug.last_seen
}

/** The verification method as the verification text describes it. There is no recorded method field. */
const METHOD_RULES: [RegExp, string][] = [
  [/\btests? pass|\bpytest\b|\bvitest\b|\bunit tests?\b|\btest suite\b/i, 'Automated test'],
  [
    /\blive (session|trading|mode|run|fills?)\b|\bbroker (fills?|records?|tradebook)\b/i,
    'Live trading session',
  ],
  [/\bsandbox\b|--mode analyze|\banalyze(r)? mode\b/i, 'Sandbox session'],
  [/\bre-?run\b|\bEOD\b|pages_vs_fills|\breconcil/i, 'Report or check re-run'],
]

export function verificationMethods(text: string): string[] {
  return METHOD_RULES.filter(([re]) => re.test(text)).map(([, label]) => label)
}

/** Commit, recorded sources, and any report path quoted in the verification text. */
export function evidenceRefs(bug: Bug): string[] {
  const quoted = bug.verification.match(/(?:reports|signal_engine|log)\/[\w./-]+(?::\d+)?/g) ?? []
  const refs = [
    ...(bug.fix_commit ? [`commit ${bug.fix_commit}`] : []),
    ...readableSources(bug),
    ...quoted,
  ]
  return [...new Set(refs)]
}
