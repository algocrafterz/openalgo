import { describe, expect, it } from 'vitest'
import { buildMonthCalendar } from '@/lib/discretionaryCalendar'
import {
  dailyTimeline,
  dayTime,
  equityRangeOptions,
  equityTimeline,
  monthsBefore,
  uncollectedDaysText,
  uncollectedInSpan,
} from '@/lib/discretionaryEquityRange'
import {
  equityCurveReconciliation,
  netEquityFromDailyRows,
  weaknessLine,
} from '@/lib/discretionaryPnlView'
import { summary } from '@/test/discretionaryFixtures'

const money = (v: number) => `Rs ${v.toFixed(2)}`

function day(period: string, net: number, extra: Record<string, unknown> = {}) {
  return {
    ...summary().by_period.daily[0],
    period,
    label: period,
    start: period,
    end: period,
    net_of_charges: net,
    net_charges_complete: true,
    charges_unavailable_trades: 0,
    ...extra,
  }
}

describe('equity curve range choices', () => {
  it('counts months back by calendar, clamped to a shorter month', () => {
    expect(monthsBefore('2026-11-23', 1)).toBe('2026-10-23')
    expect(monthsBefore('2026-11-23', 6)).toBe('2026-05-23')
    expect(monthsBefore('2026-03-31', 1)).toBe('2026-02-28')
    expect(monthsBefore('2026-02-10', 3)).toBe('2025-11-10')
  })

  it('offers every range when the history is long enough, and All starts at the first tracked date', () => {
    const o = equityRangeOptions({ today: '2026-11-23', since: '2025-01-01' })
    expect(o.map((x) => x.key)).toEqual(['period', '1m', '3m', '6m', 'all'])
    expect(o.every((x) => x.available)).toBe(true)
    expect(o.find((x) => x.key === '3m')?.range).toEqual({ start: '2026-08-23', end: '2026-11-23' })
    expect(o.find((x) => x.key === 'all')?.range).toEqual({
      start: '2025-01-01',
      end: '2026-11-23',
    })
    expect(o.find((x) => x.key === 'period')?.range).toBeNull()
  })

  it('switches off a range that reaches back before the tracked history, with the reason', () => {
    const o = equityRangeOptions({ today: '2026-11-23', since: '2026-09-15' })
    const by = Object.fromEntries(o.map((x) => [x.key, x]))
    expect(by['1m'].available).toBe(true)
    expect(by['3m'].available).toBe(false)
    expect(by['6m'].available).toBe(false)
    expect(by['3m'].reason).toBe(
      'Your tracked history starts on 2026-09-15, which is less than 3 months ago.'
    )
    expect(by.all.available).toBe(true)
  })

  it('treats a range that starts exactly on the first tracked date as available', () => {
    const o = equityRangeOptions({ today: '2026-11-23', since: '2026-10-23' })
    expect(o.find((x) => x.key === '1m')?.available).toBe(true)
  })
})

describe('day curve layout never represents a missing day as a value', () => {
  // Fri 13, Mon 16, Tue 17; Wed 18 has no row; Thu 19 has a row. Sat 14 / Sun 15 are weekend.
  const rows = [
    day('2026-11-13', 10),
    day('2026-11-16', -5),
    day('2026-11-17', 20),
    day('2026-11-19', 7),
  ]
  const curve = netEquityFromDailyRows(rows)

  it('has a gap slot (no point) for a weekday without a row, and none for a weekend', () => {
    const slots = equityTimeline(curve.points)
    expect(slots.map((s) => s.date)).toEqual([
      '2026-11-13',
      '2026-11-16',
      '2026-11-17',
      '2026-11-18',
      '2026-11-19',
    ])
    const gap = slots.find((s) => s.date === '2026-11-18')
    expect(gap?.item).toBeNull()
    // every slot with an item carries the real running total, none a zero stand-in
    expect(slots.filter((s) => s.item).map((s) => s.item?.equity)).toEqual([10, 5, 25, 32])
  })

  it('keeps the curve itself free of points for missing days', () => {
    expect(curve.points.map((p) => p.closed_at)).toEqual([
      '2026-11-13',
      '2026-11-16',
      '2026-11-17',
      '2026-11-19',
    ])
    expect(curve.points.every((p) => p.net !== 0)).toBe(true)
  })

  it('keeps a real zero-net day as a point (a row exists), but never invents one', () => {
    const withFlat = netEquityFromDailyRows([day('2026-11-16', 0), day('2026-11-18', 5)])
    expect(withFlat.points.map((p) => p.closed_at)).toEqual(['2026-11-16', '2026-11-18'])
    const slots = equityTimeline(withFlat.points)
    expect(slots.map((s) => [s.date, s.item === null])).toEqual([
      ['2026-11-16', false],
      ['2026-11-17', true],
      ['2026-11-18', false],
    ])
  })

  it('flags a not-collected weekday as a gap, and a not-collected day with a row as incomplete', () => {
    const flagged = netEquityFromDailyRows(rows, ['2026-11-18', '2026-11-16'])
    expect(flagged.points.map((p) => p.incomplete)).toEqual([false, true, false, false])
    const slots = equityTimeline(flagged.points, ['2026-11-18'])
    expect(slots.find((s) => s.date === '2026-11-18')).toMatchObject({
      item: null,
      notCollected: true,
    })
    const text = uncollectedDaysText(
      ['2026-11-16', '2026-11-18'],
      new Set(curve.points.map((p) => p.closed_at))
    )
    expect(text).toContain('not collected on 18 Nov. Those days have no point')
    expect(text).toContain('not collected on 16 Nov. Their figures are marked as incomplete')
  })

  it('counts only the not-collected days inside the shown span', () => {
    expect(
      uncollectedInSpan(['2026-10-01', '2026-11-18', '2026-12-01'], '2026-11-01', '2026-11-30')
    ).toEqual(['2026-11-18'])
    expect(uncollectedInSpan(undefined, '2026-11-01', '2026-11-30')).toEqual([])
    expect(uncollectedDaysText([], new Set())).toBeNull()
  })

  it('lays the daily bars out the same way: a gap slot has no row, so no bar', () => {
    const slots = dailyTimeline(rows, ['2026-11-18'])
    expect(slots.filter((s) => s.item === null).map((s) => s.date)).toEqual(['2026-11-18'])
    expect(slots.find((s) => s.date === '2026-11-18')?.notCollected).toBe(true)
  })

  it('converts a day to a stable UTC timestamp for the chart axis', () => {
    expect(dayTime('2026-11-16') - dayTime('2026-11-13')).toBe(3 * 86400)
  })

  it('is empty without points', () => {
    expect(equityTimeline([])).toEqual([])
  })
})

describe('calendar month total, equity-curve final value and period net reconcile', () => {
  // One set of daily net rows, three readers: they must agree on the same range.
  const rows = [
    day('2026-11-02', 120.5),
    day('2026-11-03', -45.25),
    day('2026-11-05', 30),
    day('2026-11-09', -10.75),
  ]
  const periodNet = rows.reduce((a, r) => a + r.net_of_charges, 0)
  const resp = {
    ...summary(),
    period: {
      period: 'custom',
      start: '2026-11-01',
      end: '2026-11-30',
      reliable_since: '2026-01-01',
    },
    by_period: { daily: rows, weekly: [], monthly: [] },
    sync_health: { last_successful_sync: null, runs_by_day: {}, days_without_sync: [] },
    realized_net_of_charges: periodNet,
  }

  it('shows the same figure in the calendar month, the curve end and the period net', () => {
    const month = buildMonthCalendar(resp, '2026-11', '2026-11-23').totals.net
    const curve = netEquityFromDailyRows(resp.by_period.daily).latestEquity
    expect(periodNet).toBeCloseTo(94.5, 2)
    expect(month).toBeCloseTo(periodNet, 2)
    expect(curve).toBeCloseTo(periodNet, 2)
    expect(month).toBeCloseTo(curve, 2)
    expect(
      equityCurveReconciliation({ latestEquity: curve }, resp.realized_net_of_charges, money).kind
    ).toBe('match')
  })

  it('differs legitimately when the curve range is longer than the month, and the curve then says so', () => {
    const october = day('2026-10-28', 200)
    const longer = netEquityFromDailyRows([october, ...rows]).latestEquity
    const month = buildMonthCalendar(resp, '2026-11', '2026-11-23').totals.net
    expect(longer).toBeCloseTo(month + 200, 2)
    // with its own range as the scope, the wording names that range, not the report period
    expect(
      equityCurveReconciliation({ latestEquity: longer }, longer, money, 'this range').text
    ).toContain('equals the realised net P&L of this range')
  })
})

describe('insights over a handful of trades', () => {
  it('never calls a small sample too early or low, and any biggest-trade line names the count', () => {
    const base = summary()
    const few = { ...base, metrics: { ...base.metrics, trades_count: 3 } }
    const text = weaknessLine(few, money) ?? ''
    expect(text).not.toMatch(/too early|low sample|indicative/i)
    if (/biggest/.test(text)) expect(text).toContain('over 3 completed trades')
  })
})
