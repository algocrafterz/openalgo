import { describe, expect, it } from 'vitest'
import { makeCard, makeLatest, makeRun } from '@/test/backtestResultsFixtures'
import {
  ALL,
  buildVerdict,
  coverageNotes,
  distinct,
  EDGE_T_FLOOR,
  EMPTY_FILTERS,
  edgeBar,
  evidenceClass,
  exceedsSlotLimit,
  filterIdeas,
  fmtAge,
  fmtNum,
  fmtPercent,
  fmtSigned,
  fmtSpan,
  isExtrapolated,
  isKnown,
  judgeDelta,
  metricText,
  signClass,
  signsDisagree,
  sortIdeas,
  toLineData,
  tVsHurdle,
} from './backtestResults'

describe('formatting', () => {
  it('renders unknown values as n/a, never zero', () => {
    expect(fmtNum(null)).toBe('n/a')
    expect(fmtNum(undefined)).toBe('n/a')
    expect(fmtNum(Number.NaN)).toBe('n/a')
    expect(fmtSigned(undefined)).toBe('n/a')
    expect(fmtPercent(null)).toBe('n/a')
  })

  it('signs positive numbers and formats percent', () => {
    expect(fmtSigned(1.234)).toBe('+1.23')
    expect(fmtSigned(-1.234)).toBe('-1.23')
    expect(fmtSigned(0)).toBe('0.00')
    expect(fmtPercent(0.4123)).toBe('41.2%')
  })

  it('treats a zero-trade run as unknown for every metric', () => {
    const m = { trades: 0, gross_bps: 0, net_bps: 0 }
    expect(isKnown(m, 0)).toBe(false)
    expect(metricText(m, 0)).toBe('n/a')
    expect(metricText(null, 1)).toBe('n/a')
    expect(metricText({ trades: 5 }, 2.5)).toBe('+2.50')
    expect(metricText({ trades: 5 }, undefined)).toBe('n/a')
  })

  it('colours by sign but is muted when unknown', () => {
    expect(signClass(1)).toBe('text-green-600')
    expect(signClass(-1)).toBe('text-red-600')
    expect(signClass(0)).toBe('')
    expect(signClass(1, false)).toBe('text-muted-foreground')
    expect(signClass(null)).toBe('text-muted-foreground')
  })

  it('shows t against its hurdle', () => {
    expect(tVsHurdle({ trades: 10, gross_bps: 1, net_bps: 1, t_stat: 1.9, hurdle_t: 1.97 })).toBe(
      '1.90 vs 1.97'
    )
    expect(tVsHurdle({ trades: 10, gross_bps: 1, net_bps: 1, t_stat: 1.9 })).toBe('1.90')
    expect(tVsHurdle({ trades: 10, gross_bps: 1, net_bps: 1 })).toBe('n/a')
  })

  it('flags fewer than a year of trading days as extrapolated', () => {
    expect(isExtrapolated({ trades: 1, gross_bps: 0, net_bps: 0, days: 100 })).toBe(true)
    expect(isExtrapolated({ trades: 1, gross_bps: 0, net_bps: 0, days: 252 })).toBe(false)
    expect(isExtrapolated({ trades: 1, gross_bps: 0, net_bps: 0 })).toBe(false)
  })
})

describe('sortIdeas', () => {
  const ideas = [
    makeCard({
      id: 'IDEA-001',
      verdict: 'no-edge',
      latest_run: makeLatest({ date: '2026-09-01' }),
    }),
    makeCard({ id: 'IDEA-002', verdict: 'none', run_count: 0, latest_run: null }),
    makeCard({
      id: 'IDEA-003',
      verdict: 'marginal',
      latest_run: makeLatest({ date: '2026-08-01' }),
    }),
    makeCard({
      id: 'IDEA-004',
      verdict: 'marginal',
      latest_run: makeLatest({ date: '2026-09-15' }),
    }),
    makeCard({
      id: 'IDEA-005',
      verdict: 'edge',
      latest_run: makeLatest({
        metrics: { trades: 0, gross_bps: 0, net_bps: 0 },
        oos_net_bps: null,
      }),
    }),
  ]
  const ids = (list: typeof ideas) => list.map((i) => i.id)

  it('defaults to evidence: runs first, then verdict, then newest run', () => {
    expect(ids(sortIdeas(ideas, 'evidence', 'asc'))).toEqual([
      'IDEA-005',
      'IDEA-004',
      'IDEA-003',
      'IDEA-001',
      'IDEA-002',
    ])
  })

  it('reverses evidence order on desc', () => {
    expect(ids(sortIdeas(ideas, 'evidence', 'desc'))[0]).toBe('IDEA-002')
  })

  it('does not mutate the input', () => {
    const before = ids(ideas)
    sortIdeas(ideas, 'net', 'desc')
    expect(ids(ideas)).toEqual(before)
  })

  it('sorts numeric columns with unknown values last in both directions', () => {
    const withNet = [
      makeCard({
        id: 'A',
        latest_run: makeLatest({ metrics: { trades: 5, gross_bps: 0, net_bps: 5 } }),
      }),
      makeCard({
        id: 'B',
        latest_run: makeLatest({ metrics: { trades: 5, gross_bps: 0, net_bps: -3 } }),
      }),
      makeCard({
        id: 'C',
        latest_run: makeLatest({ metrics: { trades: 0, gross_bps: 0, net_bps: 0 } }),
      }),
      makeCard({ id: 'D', run_count: 0, latest_run: null }),
    ]
    expect(ids(sortIdeas(withNet, 'net', 'desc'))).toEqual(['A', 'B', 'C', 'D'])
    expect(ids(sortIdeas(withNet, 'net', 'asc'))).toEqual(['B', 'A', 'C', 'D'])
  })

  it.each([
    ['id', 'IDEA-001'],
    ['status', 'IDEA-001'],
    ['verdict', 'IDEA-005'],
    ['date', 'IDEA-003'],
    ['runs', 'IDEA-002'],
    ['trades', 'IDEA-005'],
    ['gross', 'IDEA-001'],
    ['oos', 'IDEA-001'],
    ['trials', 'IDEA-001'],
  ] as const)('supports sorting by %s', (key, first) => {
    expect(sortIdeas(ideas, key, 'asc')[0].id).toBe(first)
  })
})

describe('filterIdeas', () => {
  const ideas = [
    makeCard({ id: 'IDEA-001', title: 'ORB breakout', tags: ['opening-range'], status: 'live' }),
    makeCard({ id: 'IDEA-002', title: 'VWAP fade', category: 'market-profile', horizon: 'swing' }),
  ]

  it('returns everything for empty filters', () => {
    expect(filterIdeas(ideas, EMPTY_FILTERS)).toHaveLength(2)
  })

  it('searches id, title, summary and tags case-insensitively', () => {
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, search: 'orb' })).toHaveLength(1)
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, search: 'OPENING' })).toHaveLength(1)
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, search: 'idea-002' })).toHaveLength(1)
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, search: 'zzz' })).toHaveLength(0)
  })

  it('filters by status, verdict, category and horizon', () => {
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, status: 'live' })).toHaveLength(1)
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, verdict: 'edge' })).toHaveLength(0)
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, category: 'market-profile' })).toHaveLength(1)
    expect(filterIdeas(ideas, { ...EMPTY_FILTERS, horizon: 'swing' })).toHaveLength(1)
    expect(EMPTY_FILTERS.status).toBe(ALL)
  })

  it('lists distinct sorted values', () => {
    expect(distinct(ideas, 'horizon')).toEqual(['intraday', 'swing'])
  })
})

describe('toLineData', () => {
  it('sorts ascending and keeps the last point per day', () => {
    const out = toLineData([
      ['2026-01-05', 2],
      ['2026-01-02', 1],
      ['2026-01-05', 3],
    ])
    expect(out).toHaveLength(2)
    expect(out[0].value).toBe(1)
    expect(out[1].value).toBe(3)
    expect(out[0].time).toBeLessThan(out[1].time)
  })

  it('handles undefined', () => {
    expect(toLineData(undefined)).toEqual([])
  })
})

describe('judgeDelta', () => {
  it('treats higher as better for returns', () => {
    expect(judgeDelta('net_bps', 2)).toBe('improved')
    expect(judgeDelta('net_bps', -2)).toBe('worse')
  })

  it('treats lower as better for drawdown', () => {
    expect(judgeDelta('max_dd_r', -3)).toBe('improved')
    expect(judgeDelta('max_dd_r', 3)).toBe('worse')
  })

  it('does not judge counts or hurdles', () => {
    expect(judgeDelta('trades', 100)).toBe('neutral')
    expect(judgeDelta('hurdle_t', 0.2)).toBe('neutral')
  })

  it('handles unchanged and missing', () => {
    expect(judgeDelta('net_bps', 0)).toBe('unchanged')
    expect(judgeDelta('net_bps', null)).toBe('n/a')
  })
})

describe('data coverage', () => {
  it('formats the span and the age of the data', () => {
    expect(fmtSpan(10)).toBe('10.0 years')
    expect(fmtSpan(5.7)).toBe('5.7 years')
    expect(fmtSpan(null)).toBe('n/a')
    expect(fmtAge(0)).toBe('same day')
    expect(fmtAge(1)).toBe('1 day old')
    expect(fmtAge(12)).toBe('12 days old')
    expect(fmtAge(undefined)).toBe('n/a')
  })

  it('warns about short history and stale data, and only then', () => {
    expect(coverageNotes({ years: 10, data_age_days: 0 })).toEqual([])
    expect(coverageNotes({ years: 2.4, data_age_days: 0 })).toEqual([
      expect.stringContaining('2.4 years'),
    ])
    expect(coverageNotes({ years: 10, data_age_days: 30 })).toEqual([
      expect.stringContaining('30 days old'),
    ])
    expect(coverageNotes({ years: 1, data_age_days: 9 })).toHaveLength(2)
  })

  it('does not warn about what it cannot measure', () => {
    expect(coverageNotes({ years: null, data_age_days: null })).toEqual([])
    expect(coverageNotes({})).toEqual([])
  })
})

describe('reality checks', () => {
  it('flags a strategy that needs more than ten positions at once', () => {
    expect(exceedsSlotLimit({ trades: 5, gross_bps: 1, net_bps: 1, peak_concurrent: 174 })).toBe(
      true
    )
    expect(exceedsSlotLimit({ trades: 5, gross_bps: 1, net_bps: 1, peak_concurrent: 10 })).toBe(
      false
    )
    expect(exceedsSlotLimit({ trades: 5, gross_bps: 1, net_bps: 1 })).toBe(false)
  })

  it('spots a per-trade result and a t-statistic that point opposite ways', () => {
    const base = { trades: 100, gross_bps: 1 }
    expect(signsDisagree({ ...base, net_bps: 17, t_stat: -2.2 })).toBe(true)
    expect(signsDisagree({ ...base, net_bps: -17, t_stat: 2.2 })).toBe(true)
    expect(signsDisagree({ ...base, net_bps: 17, t_stat: 2.2 })).toBe(false)
    expect(signsDisagree({ ...base, net_bps: 17 })).toBe(false)
  })

  it('never compares numbers of a run that made no trades', () => {
    expect(signsDisagree({ trades: 0, gross_bps: 0, net_bps: 0, t_stat: -1 })).toBe(false)
  })
})

describe('evidenceClass', () => {
  it('reserves green for a positive value that clears the bar', () => {
    expect(evidenceClass(20, 3.1, 2.5)).toBe('text-green-600')
  })
  it('shows a positive value below the bar as amber, not green', () => {
    expect(evidenceClass(22.2, -0.7, 2.5)).toBe('text-amber-600')
    expect(evidenceClass(22.2, undefined, 2.5)).toBe('text-amber-600')
  })
  it('shows a negative value as red whatever the t-statistic', () => {
    expect(evidenceClass(-5, 4, 2.5)).toBe('text-red-600')
  })
  it('is muted when the value is unknown', () => {
    expect(evidenceClass(null, 3, 2.5)).toBe('text-muted-foreground')
    expect(evidenceClass(5, 3, 2.5, false)).toBe('text-muted-foreground')
  })
})

describe('edgeBar', () => {
  it('is the floor unless the trial hurdle is higher', () => {
    expect(edgeBar({ hurdle_t: 1.97 })).toBe(EDGE_T_FLOOR)
    expect(edgeBar({ hurdle_t: 3.6 })).toBe(3.6)
    expect(edgeBar(undefined)).toBe(EDGE_T_FLOOR)
  })
})

describe('buildVerdict', () => {
  const rejected = {
    verdict: 'no-edge',
    status: 'rejected',
    rejection_basis: 'no-gross-edge',
  } as const
  const lineage = {
    runs: 4,
    latest_run_id: 'v4',
    trials_sum: 4,
    trend: 'improving',
    delta_vs_parent: null,
  } as const
  const run = (over: Record<string, unknown> = {}) =>
    makeRun({
      metrics: { trades: 3000, gross_bps: 46, net_bps: 22, t_stat: -0.7, hurdle_t: 1.97 },
      evaluation: {
        version: 1,
        dimensions: [
          {
            name: 'robustness',
            rows: [
              {
                gate: 'G4.5',
                title: 'Dispersion',
                status: 'fail',
                hard: true,
                value: 1,
                threshold: '',
                evidence: '',
              },
            ],
          },
          {
            name: 'statistical_confidence',
            rows: [
              {
                gate: 'G2.2',
                title: 'Day-clustered t-statistic',
                status: 'fail',
                hard: true,
                value: -0.7,
                threshold: '',
                evidence: '',
              },
            ],
          },
        ],
        summary: { counts: { fail: 2 }, hard_failed: ['G4.5', 'G2.2'] },
      },
      ...over,
    })

  it('names the lowest-numbered failed hard gate, not the first listed', () => {
    const v = buildVerdict(rejected, run(), lineage)
    expect(v.points.join(' ')).toContain('G2.2 Day-clustered t-statistic')
    expect(v.points.join(' ')).not.toContain('G4.5')
  })

  it('qualifies a rising trend when the level is below the bar, without server help', () => {
    const v = buildVerdict(rejected, run(), lineage)
    expect(v.points.join(' ')).toMatch(/improving, but still below the bar/)
  })

  it('does not qualify the trend once the level clears the bar', () => {
    const r = run({
      metrics: { trades: 3000, gross_bps: 60, net_bps: 40, t_stat: 3.4, hurdle_t: 1.97 },
    })
    expect(
      buildVerdict({ verdict: 'edge', status: 'backtested' }, r, lineage).points.join(' ')
    ).not.toMatch(/below the bar/)
  })

  it('flags a thin sample', () => {
    const r = run({ metrics: { trades: 114, gross_bps: 52, net_bps: 28, t_stat: 0.5 } })
    expect(buildVerdict(rejected, r, null).points.join(' ')).toContain('Only 114 trades')
  })

  it('rejected ideas are told to stop, open ones are told the bar is not cleared', () => {
    expect(buildVerdict(rejected, run(), null).tone).toBe('fail')
    const open = buildVerdict(
      { verdict: 'none', status: 'backtested' },
      run({ evaluation: undefined }),
      null
    )
    expect(open.tone).toBe('warn')
  })
})
