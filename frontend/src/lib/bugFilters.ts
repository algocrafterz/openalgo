import type { Bug, BugKind, BugSeverity, BugStatus } from '@/api/bugs'
import { type BugCategoryKey, type BugGroup, categoryOfBug, groupOf, lastUpdated } from './bugModel'

export type { BugGroup }
export { groupOf }

export type BugSort = 'severity' | 'recent' | 'updated' | 'oldest' | 'frequent'

export const SEVERITY_RANK: Record<BugSeverity, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 }

export interface BugFilters {
  query: string
  severity: BugSeverity | 'all'
  kind: BugKind | 'all'
  area: string
  category: BugCategoryKey | 'all'
  strategy: string
  status: BugStatus | 'all'
  /** High risk only: severity HIGH. */
  highRisk: boolean
}

export const NO_FILTERS: BugFilters = {
  query: '',
  severity: 'all',
  kind: 'all',
  area: 'all',
  category: 'all',
  strategy: 'all',
  status: 'all',
  highRisk: false,
}

export function isFiltering(f: BugFilters): boolean {
  return (Object.keys(NO_FILTERS) as (keyof BugFilters)[]).some((k) => f[k] !== NO_FILTERS[k])
}

/** Every word typed must appear somewhere in the fields a trader or reviewer would search. */
export function matchesQuery(bug: Bug, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (words.length === 0) return true
  const haystack = [
    bug.id,
    bug.title,
    bug.plain ?? '',
    bug.area,
    bug.impact,
    bug.root_cause,
    bug.fix,
    ...bug.strategies,
  ]
    .join(' ')
    .toLowerCase()
  return words.every((w) => haystack.includes(w))
}

export function matchesFilters(bug: Bug, f: BugFilters): boolean {
  if (f.highRisk && bug.severity !== 'HIGH') return false
  if (f.severity !== 'all' && bug.severity !== f.severity) return false
  if (f.kind !== 'all' && bug.kind !== f.kind) return false
  if (f.area !== 'all' && bug.area !== f.area) return false
  if (f.status !== 'all' && bug.status !== f.status) return false
  if (f.strategy !== 'all' && !bug.strategies.includes(f.strategy)) return false
  if (f.category !== 'all' && categoryOfBug(bug) !== f.category) return false
  return matchesQuery(bug, f.query)
}

export function countByGroup(bugs: Bug[], f: BugFilters): Record<BugGroup, number> {
  const counts: Record<BugGroup, number> = { todo: 0, proving: 0, done: 0 }
  for (const bug of bugs) if (matchesFilters(bug, f)) counts[groupOf(bug)] += 1
  return counts
}

export function sortBugs(bugs: Bug[], sort: BugSort): Bug[] {
  const bySeverity = (a: Bug, b: Bug) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]
  const byRecent = (a: Bug, b: Bug) => b.last_seen.localeCompare(a.last_seen)
  const byUpdated = (a: Bug, b: Bug) => lastUpdated(b).localeCompare(lastUpdated(a))
  const byOldest = (a: Bug, b: Bug) => a.first_seen.localeCompare(b.first_seen)
  const byFrequent = (a: Bug, b: Bug) => b.occurrences.length - a.occurrences.length
  const order = {
    severity: [bySeverity, byRecent],
    recent: [byRecent, bySeverity],
    updated: [byUpdated, bySeverity],
    oldest: [byOldest, bySeverity],
    frequent: [byFrequent, bySeverity, byRecent],
  }[sort]
  return [...bugs].sort((a, b) => {
    for (const compare of order) {
      const result = compare(a, b)
      if (result !== 0) return result
    }
    return a.id.localeCompare(b.id)
  })
}

export function visibleBugs(
  bugs: Bug[],
  group: BugGroup,
  filters: BugFilters,
  sort: BugSort
): Bug[] {
  return sortBugs(
    bugs.filter((b) => groupOf(b) === group && matchesFilters(b, filters)),
    sort
  )
}

export function distinctAreas(bugs: Bug[]): string[] {
  return [...new Set(bugs.map((b) => b.area).filter(Boolean))].sort()
}

export function distinctStrategies(bugs: Bug[]): string[] {
  return [...new Set(bugs.flatMap((b) => b.strategies))].sort()
}
