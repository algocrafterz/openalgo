import type { WorkflowStep } from '@/api/opsWorkflow'

/** Numbers behind the Daily Workflow charts. Nothing here is estimated: it only rescales what the
 * status files and checks recorded. */

function seconds(hms: string): number {
  const [h, m, s] = hms.split(':').map(Number)
  return h * 3600 + m * 60 + s
}

export function stepSeconds(step: Pick<WorkflowStep, 'started' | 'ended'>): number {
  return Math.max(0, seconds(step.ended) - seconds(step.started))
}

export function formatDuration(total: number): string {
  if (total >= 3600) return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`
  if (total >= 60) return `${Math.floor(total / 60)}m ${total % 60}s`
  return `${total}s`
}

export interface StepBar {
  step: string
  seconds: number
  /** Width as a share of the longest step in the checkpoint, never below a visible sliver. */
  pct: number
}

export function stepBars(steps: Pick<WorkflowStep, 'step' | 'started' | 'ended'>[]): StepBar[] {
  const secs = steps.map(stepSeconds)
  const longest = Math.max(1, ...secs)
  return steps.map((s, i) => ({
    step: s.step,
    seconds: secs[i],
    pct: Math.max(3, Math.round((secs[i] / longest) * 100)),
  }))
}

export interface DayBar {
  day: string
  critical: number
  warning: number
  /** Heights, 0 to 100, scaled to the worst day in the strip. */
  criticalPct: number
  warningPct: number
  hasData: boolean
}

/** Oldest first, so the strip reads left to right in time. `counts` is newest first, as listed. */
export function dayBars(
  counts: { day: string; critical: number; warning: number; hasData: boolean }[]
): DayBar[] {
  const ordered = [...counts].reverse()
  const tallest = Math.max(1, ...ordered.map((c) => c.critical + c.warning))
  return ordered.map((c) => ({
    ...c,
    // A failed check must stay visible beside a long list of warnings.
    criticalPct: c.critical ? Math.max(12, Math.round((c.critical / tallest) * 100)) : 0,
    warningPct: Math.round((c.warning / tallest) * 100),
  }))
}
