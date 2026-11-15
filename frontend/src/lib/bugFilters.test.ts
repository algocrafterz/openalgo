import { describe, expect, it } from 'vitest'
import type { Bug } from '@/api/bugs'
import {
  countByGroup,
  countByPageGroup,
  distinctAreas,
  distinctStrategies,
  groupOf,
  isFiltering,
  matchesQuery,
  NO_FILTERS,
  overdueCount,
  pageGroupOf,
  parsePageGroup,
  sortBugs,
  visibleBugs,
  visiblePage,
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
  it('searches owner, evidence, history, verification and its reference', () => {
    const b = bug({
      owner: 'Priya',
      evidence: ['trades.db row 3501 booked under stop 2611'],
      history: [
        {
          at: '2026-11-02T10:00:00+05:30',
          status: 'open',
          note: 'severity MEDIUM -> HIGH: wrong order risk',
        },
      ],
      verification: 'held across five sessions',
      verification_ref: { kind: 'test', ref: 'signal_engine/tests/test_stops.py::test_restart' },
      fix_commit: 'abc1234',
      fix_explanation: 'sweeps orphan stops',
    })
    for (const q of [
      'priya',
      'row 3501',
      'wrong order',
      'five sessions',
      'test_restart',
      'abc1234',
      'orphan',
    ])
      expect(matchesQuery(b, q)).toBe(true)
    expect(matchesQuery(b, 'nobody-here')).toBe(false)
    expect(matchesQuery(bug({ owner: 'Priya' }), 'ravi')).toBe(false)
  })
  it('still works for an old record with no owner, reference or history lists', () => {
    const old = bug({ owner: undefined, evidence: undefined as never, history: undefined as never })
    expect(matchesQuery(old, 'title')).toBe(true)
    expect(matchesQuery(old, 'priya')).toBe(false)
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

describe('page groups', () => {
  it('splits work into needs attention, being fixed, awaiting verification and done', () => {
    expect(SAMPLE.map(pageGroupOf)).toEqual(['attention', 'fixing', 'proving', 'done', 'done'])
    expect(countByPageGroup(SAMPLE, NO_FILTERS)).toEqual({
      attention: 1,
      fixing: 1,
      proving: 1,
      done: 2,
    })
  })

  it('keeps the old todo link working and defaults to needs attention', () => {
    expect(parsePageGroup('todo')).toBe('attention')
    expect(parsePageGroup(null)).toBe('attention')
    expect(parsePageGroup('nonsense')).toBe('attention')
    expect(parsePageGroup('fixing')).toBe('fixing')
    expect(parsePageGroup('proving')).toBe('proving')
    expect(parsePageGroup('done')).toBe('done')
  })

  it('lists overdue bugs first, then the chosen sort', () => {
    const list = [
      bug({ id: 'BUG-010', severity: 'HIGH', status: 'open' }),
      bug({ id: 'BUG-011', severity: 'LOW', status: 'open', overdue: true }),
      bug({ id: 'BUG-012', severity: 'MEDIUM', status: 'open' }),
      bug({
        id: 'BUG-013',
        severity: 'LOW',
        status: 'open',
        overdue: true,
        first_seen: '2026-10-01',
      }),
    ]
    expect(visiblePage(list, 'attention', NO_FILTERS, 'severity').map((b) => b.id)).toEqual([
      'BUG-011',
      'BUG-013',
      'BUG-010',
      'BUG-012',
    ])
    expect(visiblePage(list, 'attention', NO_FILTERS, 'oldest').map((b) => b.id)[0]).toBe('BUG-013')
    expect(overdueCount(list)).toBe(2)
    expect(visiblePage(list, 'fixing', NO_FILTERS, 'severity')).toEqual([])
  })

  it('applies filters inside a group', () => {
    const rows = visiblePage(SAMPLE, 'done', { ...NO_FILTERS, status: 'wontfix' }, 'severity')
    expect(rows.map((b) => b.id)).toEqual(['BUG-005'])
  })
})
