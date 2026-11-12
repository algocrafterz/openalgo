import { describe, expect, it } from 'vitest'
import type { Bug } from '@/api/bugs'
import { seenPerDay, statusBreakdown } from './bugCharts'

const bug = (over: Partial<Bug>): Bug =>
  ({
    id: 'BUG-1',
    status: 'open',
    severity: 'LOW',
    occurrences: [],
    ...over,
  }) as Bug

describe('statusBreakdown', () => {
  it('counts every status in lifecycle order and the shares add to 100', () => {
    const rows = statusBreakdown([
      bug({ status: 'open' }),
      bug({ status: 'open' }),
      bug({ status: 'fixed' }),
      bug({ status: 'verified' }),
    ])
    expect(rows.map((r) => [r.status, r.count])).toEqual([
      ['open', 2],
      ['in_progress', 0],
      ['fixed', 1],
      ['verified', 1],
      ['wontfix', 0],
    ])
    expect(rows.reduce((n, r) => n + r.pct, 0)).toBeCloseTo(100)
  })

  it('is all zero with no bugs', () => {
    expect(statusBreakdown([]).every((r) => r.count === 0 && r.pct === 0)).toBe(true)
  })
})

describe('seenPerDay', () => {
  it('lists n calendar days ending on the given day, oldest first', () => {
    const days = seenPerDay([], '2026-11-12', 14)
    expect(days).toHaveLength(14)
    expect(days[0].day).toBe('2026-10-30')
    expect(days[13].day).toBe('2026-11-12')
  })

  it('counts distinct bugs per day and how many are high risk', () => {
    const days = seenPerDay(
      [
        bug({ id: 'A', severity: 'HIGH', occurrences: ['2026-11-04', '2026-11-05'] }),
        bug({ id: 'B', occurrences: ['2026-11-04'] }),
      ],
      '2026-11-05',
      3
    )
    expect(days.map((d) => [d.day, d.count, d.high])).toEqual([
      ['2026-11-03', 0, 0],
      ['2026-11-04', 2, 1],
      ['2026-11-05', 1, 1],
    ])
  })
})
