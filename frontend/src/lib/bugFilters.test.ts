import { describe, expect, it } from 'vitest'
import type { Bug } from '@/api/bugs'
import {
  countByGroup,
  distinctAreas,
  distinctStrategies,
  groupOf,
  isFiltering,
  matchesQuery,
  NO_FILTERS,
  sortBugs,
  visibleBugs,
} from './bugFilters'

function bug(over: Partial<Bug>): Bug {
  return {
    id: 'BUG-001',
    title: 'Title',
    plain: '',
    kind: 'BUG',
    severity: 'LOW',
    status: 'open',
    area: 'signal_engine',
    strategies: [],
    first_seen: '2026-11-01',
    last_seen: '2026-11-01',
    occurrences: ['2026-11-01'],
    impact: '',
    evidence: [],
    root_cause: '',
    fix: '',
    fix_commit: '',
    verification: '',
    sources: [],
    history: [],
    created_at: '',
    updated_at: '',
    ...over,
  }
}

const SAMPLE = [
  bug({ id: 'BUG-001', severity: 'LOW', last_seen: '2026-11-03', status: 'open' }),
  bug({
    id: 'BUG-002',
    severity: 'HIGH',
    last_seen: '2026-11-01',
    status: 'in_progress',
    strategies: ['ORB'],
    area: 'sandbox',
    occurrences: ['2026-11-01', '2026-11-02', '2026-11-03'],
  }),
  bug({ id: 'BUG-003', severity: 'HIGH', last_seen: '2026-11-02', status: 'fixed' }),
  bug({ id: 'BUG-004', severity: 'MEDIUM', status: 'verified', kind: 'DATA' }),
  bug({ id: 'BUG-005', severity: 'MEDIUM', status: 'wontfix' }),
]

describe('groupOf', () => {
  it('maps every status to its group', () => {
    expect(SAMPLE.map(groupOf)).toEqual(['todo', 'todo', 'proving', 'done', 'done'])
  })
})

describe('matchesQuery', () => {
  it('matches an id, a strategy and an area, case-insensitively', () => {
    expect(matchesQuery(SAMPLE[1], 'bug-002')).toBe(true)
    expect(matchesQuery(SAMPLE[1], 'orb')).toBe(true)
    expect(matchesQuery(SAMPLE[1], 'SANDBOX')).toBe(true)
  })
  it('requires every word', () => {
    expect(matchesQuery(SAMPLE[1], 'orb sandbox')).toBe(true)
    expect(matchesQuery(SAMPLE[1], 'orb nothing')).toBe(false)
  })
  it('treats a blank query as match-all', () => {
    expect(matchesQuery(SAMPLE[0], '   ')).toBe(true)
  })
})

describe('counts and visibility', () => {
  it('counts per group and narrows with a filter', () => {
    expect(countByGroup(SAMPLE, NO_FILTERS)).toEqual({ todo: 2, proving: 1, done: 2 })
    expect(countByGroup(SAMPLE, { ...NO_FILTERS, severity: 'HIGH' })).toEqual({
      todo: 1,
      proving: 1,
      done: 0,
    })
    expect(countByGroup(SAMPLE, { ...NO_FILTERS, kind: 'DATA' })).toEqual({
      todo: 0,
      proving: 0,
      done: 1,
    })
  })
  it('shows only the chosen group', () => {
    const ids = visibleBugs(SAMPLE, 'todo', NO_FILTERS, 'severity').map((b) => b.id)
    expect(ids).toEqual(['BUG-002', 'BUG-001'])
  })
})

describe('sortBugs', () => {
  it('sorts by severity, then most recent', () => {
    expect(sortBugs(SAMPLE, 'severity').map((b) => b.id)).toEqual([
      'BUG-003',
      'BUG-002',
      'BUG-004',
      'BUG-005',
      'BUG-001',
    ])
  })
  it('sorts by most recent first', () => {
    expect(sortBugs(SAMPLE, 'recent')[0].id).toBe('BUG-001')
  })
  it('sorts by days seen', () => {
    expect(sortBugs(SAMPLE, 'frequent')[0].id).toBe('BUG-002')
  })
  it('does not mutate its input', () => {
    const before = SAMPLE.map((b) => b.id)
    sortBugs(SAMPLE, 'recent')
    expect(SAMPLE.map((b) => b.id)).toEqual(before)
  })
})

describe('distinctAreas', () => {
  it('lists each area once, sorted', () => {
    expect(distinctAreas(SAMPLE)).toEqual(['sandbox', 'signal_engine'])
  })
})

describe('new filters', () => {
  it('filters high risk only, by strategy, by status and by category', () => {
    expect(countByGroup(SAMPLE, { ...NO_FILTERS, highRisk: true })).toEqual({
      todo: 1,
      proving: 1,
      done: 0,
    })
    expect(countByGroup(SAMPLE, { ...NO_FILTERS, strategy: 'ORB' }).todo).toBe(1)
    expect(countByGroup(SAMPLE, { ...NO_FILTERS, status: 'wontfix' })).toEqual({
      todo: 0,
      proving: 0,
      done: 1,
    })
    expect(countByGroup(SAMPLE, { ...NO_FILTERS, category: 'data' }).done).toBe(1)
  })
  it('knows when a filter is on', () => {
    expect(isFiltering(NO_FILTERS)).toBe(false)
    expect(isFiltering({ ...NO_FILTERS, highRisk: true })).toBe(true)
  })
  it('lists strategies once, sorted', () => {
    expect(distinctStrategies(SAMPLE)).toEqual(['ORB'])
  })
  it('sorts oldest unresolved first and recently updated first', () => {
    const a = bug({ id: 'A', first_seen: '2026-10-01', updated_at: '2026-11-01T10:00:00' })
    const b = bug({ id: 'B', first_seen: '2026-10-20', updated_at: '2026-11-03T10:00:00' })
    expect(sortBugs([b, a], 'oldest').map((x) => x.id)).toEqual(['A', 'B'])
    expect(sortBugs([a, b], 'updated').map((x) => x.id)).toEqual(['B', 'A'])
  })
})
