import type {
  Bug,
  BugActionBody,
  BugActionKey,
  BugSeverity,
  BugStatus,
  BugVerificationKind,
  BugVerificationRef,
} from '@/api/bugs'

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

/** Server-computed age in whole days, or null when the server could not compute it. */
export function ageDays(bug: Bug): number | null {
  return typeof bug.age_days === 'number' ? bug.age_days : null
}

export function ageLabel(bug: Bug): string {
  const n = ageDays(bug)
  if (n === null) return 'age unknown'
  if (n <= 0) return 'today'
  return n === 1 ? '1 day old' : `${n} days old`
}

export function isOverdue(bug: Bug): boolean {
  return bug.overdue === true
}

export function ownerLabel(bug: Bug): string {
  return bug.owner?.trim() ? bug.owner.trim() : 'Unassigned'
}

const FALLBACK_NEXT: Record<BugStatus, string> = {
  open: 'Assign and start',
  in_progress: 'Finish fix',
  fixed: 'Verify with evidence',
  verified: 'None',
  wontfix: 'None',
}

/** The server's next action; the status-based fallback only covers an older server. */
export function nextAction(bug: Bug): string {
  return bug.next_action || FALLBACK_NEXT[bug.status]
}

/**
 * Which actions a bug's status offers. This only decides which buttons to show; the server's
 * transition table is the authority and refuses anything else.
 */
const ACTIONS_BY_STATUS: Record<BugStatus, BugActionKey[]> = {
  open: ['start', 'assign', 'note', 'wontfix'],
  in_progress: ['fix', 'assign', 'note', 'reopen', 'wontfix'],
  fixed: ['verify', 'start', 'assign', 'note', 'reopen'],
  verified: ['note', 'reopen'],
  wontfix: ['note', 'reopen'],
}

export function availableActions(status: BugStatus): BugActionKey[] {
  return ACTIONS_BY_STATUS[status]
}

/** The free-text body fields; the risk level and the reference are built separately. */
export type TextBodyKey = Exclude<keyof BugActionBody, 'severity' | 'verification_ref'>

export interface ActionField {
  key: TextBodyKey
  label: string
  required: boolean
  multiline: boolean
  hint?: string
}

export interface ActionSpec {
  label: string
  title: string
  description: string
  submit: string
  fields: ActionField[]
}

export const ACTION_SPECS: Record<BugActionKey, ActionSpec> = {
  assign: {
    label: 'Assign',
    title: 'Assign an owner',
    description: 'Who is responsible for this bug. It is recorded in the history.',
    submit: 'Assign',
    fields: [{ key: 'owner', label: 'Owner', required: true, multiline: false }],
  },
  start: {
    label: 'Start fix',
    title: 'Start working on this bug',
    description: 'Moves it to Being fixed. An owner and a note are optional.',
    submit: 'Start',
    fields: [
      { key: 'owner', label: 'Owner', required: false, multiline: false },
      { key: 'text', label: 'Note', required: false, multiline: true },
    ],
  },
  note: {
    label: 'Add note',
    title: 'Add a note',
    description: 'Adds a line to the history without changing the status.',
    submit: 'Add note',
    fields: [{ key: 'text', label: 'Note', required: true, multiline: true }],
  },
  fix: {
    label: 'Mark fixed',
    title: 'Mark as fixed',
    description:
      'Describe what was changed. The bug then waits in Awaiting verification until evidence proves it.',
    submit: 'Mark fixed',
    fields: [
      { key: 'fix', label: 'What was changed', required: true, multiline: true },
      { key: 'commit', label: 'Commit (optional)', required: false, multiline: false },
      {
        key: 'fix_explanation',
        label: 'Why it works (optional)',
        required: false,
        multiline: true,
      },
    ],
  },
  verify: {
    label: 'Verify',
    title: 'Verify with evidence',
    description:
      'Name the clean session, report or test that proves the fix. Without evidence it stays unverified.',
    submit: 'Mark verified',
    fields: [{ key: 'evidence', label: 'Evidence', required: true, multiline: true }],
  },
  reopen: {
    label: 'Reopen',
    title: 'Reopen this bug',
    description: 'Moves it back to Needs attention. A reason is required.',
    submit: 'Reopen',
    fields: [{ key: 'reason', label: 'Reason', required: true, multiline: true }],
  },
  wontfix: {
    label: "Won't fix",
    title: 'Close without fixing',
    description: 'The bug is closed on purpose. A reason is required.',
    submit: "Won't fix",
    fields: [{ key: 'reason', label: 'Reason', required: true, multiline: true }],
  },
  // Not listed in ACTIONS_BY_STATUS: changing the risk level is open to every status.
  severity: {
    label: 'Change risk',
    title: 'Change the risk level',
    description: 'Pick the new level and say why. The change is recorded in the history.',
    submit: 'Change risk',
    fields: [{ key: 'reason', label: 'Reason', required: true, multiline: true }],
  },
}

export const VERIFICATION_KINDS: { value: BugVerificationKind; label: string }[] = [
  { value: 'test', label: 'Test' },
  { value: 'session', label: 'Session' },
  { value: 'command', label: 'Command' },
  { value: 'commit', label: 'Commit' },
]

export const MAX_VERIFICATION_REF = 300

/** The optional proof pointer: nothing when the reference is blank, an error when it is too long. */
export function buildVerificationRef(
  kind: BugVerificationKind,
  text: string
): { ok: true; ref?: BugVerificationRef } | { ok: false; error: string } {
  const ref = text.trim()
  if (ref === '') return { ok: true }
  if (ref.length > MAX_VERIFICATION_REF)
    return { ok: false, error: `The reference is too long (at most ${MAX_VERIFICATION_REF}).` }
  return { ok: true, ref: { kind, ref } }
}

/** The risk-change body: a different level and a reason are both required. */
export function buildSeverityBody(
  current: BugSeverity,
  next: BugSeverity,
  reason: string
): { ok: true; body: BugActionBody } | { ok: false; error: string } {
  if (next === current) return { ok: false, error: `It is already ${next.toLowerCase()} risk.` }
  const text = reason.trim()
  if (text === '') return { ok: false, error: 'Reason is required.' }
  return { ok: true, body: { severity: next, reason: text } }
}

export type ActionForm = Partial<Record<TextBodyKey, string>>

/** The request body, or the first missing required field's message. Empty optional fields are dropped. */
export function buildActionBody(
  action: BugActionKey,
  form: ActionForm
): { ok: true; body: BugActionBody } | { ok: false; error: string } {
  const body: BugActionBody = {}
  for (const f of ACTION_SPECS[action].fields) {
    const value = (form[f.key] ?? '').trim()
    if (f.required && value === '') return { ok: false, error: `${f.label} is required.` }
    if (value !== '') body[f.key] = value
  }
  return { ok: true, body }
}

/**
 * Give focus back to the element that opened a dialog, if it is still on the page (a saved
 * action can remove its own button). Returns whether focus was moved.
 */
export function returnFocus(el: { isConnected?: boolean; focus?: () => void } | null): boolean {
  if (!el || el.isConnected === false || typeof el.focus !== 'function') return false
  el.focus()
  return true
}
