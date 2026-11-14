import type {
  WorkflowDay,
  WorkflowNotifications,
  WorkflowRecurrenceItem,
  WorkflowRunStatus,
} from '@/api/opsWorkflow'

/**
 * Presentation logic for R4.1: expected-versus-completed runs, recurring failures and the last
 * alert. Pure functions over what the server returned; nothing is measured here.
 */

export type RunTone = 'good' | 'warn' | 'bad' | 'idle'

export interface RunLabel {
  label: string
  tone: RunTone
  /** True when the state says the phase did not (fully) run and needs a look. */
  problem: boolean
}

const STATE_LABEL: Record<string, RunLabel> = {
  completed: { label: 'Completed', tone: 'good', problem: false },
  running: { label: 'Running', tone: 'idle', problem: false },
  failed: { label: 'Failed', tone: 'bad', problem: true },
  timed_out: { label: 'Timed out', tone: 'bad', problem: true },
  interrupted: { label: 'Interrupted', tone: 'bad', problem: true },
  missed: { label: 'Missed', tone: 'bad', problem: true },
  unreadable: { label: 'Record unreadable', tone: 'warn', problem: true },
  legacy: { label: 'Ran (no run record)', tone: 'idle', problem: false },
  not_due: { label: 'Not due yet', tone: 'idle', problem: false },
  not_expected: { label: 'Not expected today', tone: 'idle', problem: false },
}

/** Label for a run row. A completed run that started late reads "Completed late". */
export function runLabel(run: WorkflowRunStatus | undefined): RunLabel | null {
  if (!run) return null
  const base = STATE_LABEL[run.state] ?? { label: run.state, tone: 'idle' as const, problem: false }
  if (run.late && !base.problem && run.state !== 'running') {
    return { label: `${base.label}, started late`, tone: 'warn', problem: true }
  }
  if (run.late && base.problem) return { ...base, label: `${base.label}, started late` }
  return base
}

export interface RunSummary {
  expected: number
  completed: number
  /** Phases that were missed, interrupted, failed, timed out, unreadable or started late. */
  problems: { phase: string; due: string; label: string }[]
  text: string
}

/** "2 of 3 expected runs completed" plus the list of phases that need a look. */
export function summarizeRuns(rows: WorkflowRunStatus[] | undefined): RunSummary | null {
  if (!rows || rows.length === 0) return null
  const expected = rows.filter((r) => r.state !== 'not_expected' && r.state !== 'not_due')
  if (expected.length === 0) {
    const none = rows.every((r) => r.state === 'not_expected')
    return {
      expected: 0,
      completed: 0,
      problems: [],
      text: none ? 'No runs expected today.' : 'No run is due yet.',
    }
  }
  const completed = expected.filter((r) => r.state === 'completed' || r.state === 'legacy')
  const problems = rows.flatMap((r) => {
    const l = runLabel(r)
    return l?.problem ? [{ phase: r.phase, due: r.due, label: l.label }] : []
  })
  return {
    expected: expected.length,
    completed: completed.length,
    problems,
    text: `${completed.length} of ${expected.length} expected runs completed`,
  }
}

/** Used only when the server sends no schedule (an older server, or one that could not read it). */
const FALLBACK_TIMES: Record<string, string> = {
  'open-day': '09:12',
  liveness: '09:27',
  'close-day': '15:36',
}

const HHMM = /^\d{2}:\d{2}$/

/** The HH:MM a phase is scheduled at: the server's schedule first, the built-in time otherwise. */
export function phaseTime(day: WorkflowDay | null | undefined, phase: string): string {
  const served = day?.schedule?.phases?.[phase]
  return typeof served === 'string' && HHMM.test(served) ? served : (FALLBACK_TIMES[phase] ?? '')
}

export function runFor(day: WorkflowDay, phase: string): WorkflowRunStatus | undefined {
  return day.run_status?.find((r) => r.phase === phase)
}

const MASK = /\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(:\d{2})?/g

/** The same normalisation the server uses, so a check whose name carries a date still matches. */
export function recurrenceKey(group: string, name: string): string {
  return `${group}|${name.replace(MASK, '#').split(/\s+/).filter(Boolean).join(' ').toLowerCase()}`
}

/** The recurrence entry of a failed check, or undefined when the server did not send one. */
export function recurrenceOf(
  day: WorkflowDay,
  group: string,
  name: string
): WorkflowRecurrenceItem | undefined {
  const key = recurrenceKey(group, name)
  return day.recurrence?.items.find((i) => recurrenceKey(i.group, i.name) === key)
}

/** "Failed 3 of the last 10 sessions (first 2026-11-03, last 2026-11-13)". */
export function recurrenceText(item: WorkflowRecurrenceItem, window: number): string {
  const span = item.count > 1 ? ` (first ${item.first}, last ${item.last})` : ''
  return `Failed ${item.count} of the last ${window} sessions${span}`
}

/** Checks that failed on 2+ of the window's sessions, most frequent first. */
export function recurringFailures(day: WorkflowDay): WorkflowRecurrenceItem[] {
  return (day.recurrence?.items ?? []).filter((i) => i.count >= 2)
}

export interface NoticeLine {
  label: string
  value: string
}

/** Rows for the notification panel, or null when the server sent none. */
export function noticeLines(n: WorkflowNotifications | undefined): NoticeLine[] | null {
  if (!n) return null
  if (!n.available) return [{ label: 'Alerts', value: n.reason ?? 'Not available' }]
  const when = (v?: string | null) => (v ? v.replace('T', ' ').slice(0, 16) : 'none recorded')
  const lines: NoticeLine[] = [
    {
      label: 'Last alert attempt',
      value: n.last_attempt_at
        ? `${when(n.last_attempt_at)}, ${(n.last_result ?? '').replace(/_/g, ' ')}`
        : 'none recorded',
    },
    { label: 'Last delivered', value: when(n.last_sent_at) },
    {
      label: 'Open red alerts',
      value: n.open_red?.length ? n.open_red.join('; ') : 'none',
    },
    {
      label: 'Last recovery',
      value: n.last_recovery_at
        ? `${when(n.last_recovery_at)}${n.last_recovery_key ? `, ${n.last_recovery_key}` : ''}`
        : 'none recorded',
    },
  ]
  return lines
}

/** True when the last attempt failed or a red alert is open, so the panel should be visible. */
export function noticeNeedsLook(n: WorkflowNotifications | undefined): boolean {
  if (!n?.available) return false
  return Boolean(n.open_red?.length) || (n.last_result ?? '').startsWith('failed')
}
