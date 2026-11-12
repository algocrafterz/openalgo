import type { Bug, BugStatus } from '@/api/bugs'

/** Numbers behind the Bug Tracker charts, from the persisted status and occurrence days only. */

export const STATUS_ORDER: BugStatus[] = ['open', 'in_progress', 'fixed', 'verified', 'wontfix']

export interface StatusShare {
  status: BugStatus
  count: number
  pct: number
}

export function statusBreakdown(bugs: Bug[]): StatusShare[] {
  const total = bugs.length
  return STATUS_ORDER.map((status) => {
    const count = bugs.filter((b) => b.status === status).length
    return { status, count, pct: total ? (count / total) * 100 : 0 }
  })
}

export interface SeenDay {
  day: string
  /** Distinct bugs that happened on this day. */
  count: number
  /** How many of those are HIGH risk. */
  high: number
}

function addDays(day: string, delta: number): string {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + delta)
  return d.toISOString().slice(0, 10)
}

/** The last `n` calendar days ending on `end`, oldest first, with how many bugs were seen each day. */
export function seenPerDay(bugs: Bug[], end: string, n = 14): SeenDay[] {
  const days = Array.from({ length: n }, (_, i) => addDays(end, i - (n - 1)))
  return days.map((day) => {
    const seen = bugs.filter((b) => b.occurrences.includes(day))
    return { day, count: seen.length, high: seen.filter((b) => b.severity === 'HIGH').length }
  })
}
