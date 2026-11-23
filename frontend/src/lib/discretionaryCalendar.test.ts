import { describe, expect, it } from 'vitest'
import type { PeriodRow } from '@/api/discretionaryPnl'
import {
  buildMonthCalendar,
  canGoNext,
  canGoPrevious,
  monthRange,
  monthTitle,
  shiftMonth,
} from '@/lib/discretionaryCalendar'
import { summary } from '@/test/discretionaryFixtures'

function row(date: string, net: number, extra: Partial<PeriodRow> = {}): PeriodRow {
  return {
    period: date,
    label: date,
    start: date,
    end: date,
    trades: 2,
    wins: net > 0 ? 2 : 0,
    losses: net > 0 ? 0 : 2,
    realized_gross: net + 10,
    charges: 10,
    charges_unavailable_trades: 0,
    net_of_charges: net,
    net_charges_complete: true,
    ...extra,
  }
}

function monthSummary(
  rows: PeriodRow[],
  extra: { days_without_sync?: string[]; start?: string } = {}
) {
  const base = summary()
  return {
    ...base,
    period: { ...base.period, start: extra.start ?? '2026-11-01', end: '2026-11-30' },
    sync_health: { ...base.sync_health, days_without_sync: extra.days_without_sync ?? [] },
    by_period: { daily: rows, weekly: [], monthly: [] },
  }
}

describe('buildMonthCalendar', () => {
  it('sums the days shown, so the month total reconciles with the daily values', () => {
    const rows = [
      row('2026-11-02', 100.1),
      row('2026-11-03', -40.2),
      row('2026-11-04', 0.3),
      row('2026-11-05', -25.55),
    ]
    const cal = buildMonthCalendar(monthSummary(rows), '2026-11', '2026-11-23')
    const daySum = cal.days.reduce((s, d) => s + (d.net ?? 0), 0)
    expect(cal.totals.net).toBeCloseTo(daySum, 2)
    expect(cal.totals.net).toBeCloseTo(
      rows.reduce((s, r) => s + r.net_of_charges, 0),
      2
    )
    expect(cal.totals.tradingDays).toBe(4)
    expect(cal.totals.profitableDays).toBe(2)
    expect(cal.totals.losingDays).toBe(2)
    expect(cal.totals.trades).toBe(8)
    expect(cal.days).toHaveLength(30)
  })

  it('colours by sign and keeps trade count and win / loss per day', () => {
    const cal = buildMonthCalendar(
      monthSummary([row('2026-11-02', 50), row('2026-11-03', -30), row('2026-11-04', 0)]),
      '2026-11',
      '2026-11-23'
    )
    const by = (d: string) => cal.days.find((x) => x.date === d)
    expect(by('2026-11-02')).toMatchObject({ status: 'profit', trades: 2, wins: 2, losses: 0 })
    expect(by('2026-11-03')).toMatchObject({ status: 'loss', wins: 0, losses: 2 })
    expect(by('2026-11-04')?.status).toBe('flat')
    expect(cal.totals.flatDays).toBe(1)
  })

  it('ignores daily rows of another month', () => {
    const cal = buildMonthCalendar(
      monthSummary([row('2026-10-30', 999), row('2026-11-02', 10)]),
      '2026-11',
      '2026-11-23'
    )
    expect(cal.totals.net).toBe(10)
    expect(cal.totals.tradingDays).toBe(1)
  })

  it('tells a day with no trades from a day with incomplete data', () => {
    const cal = buildMonthCalendar(
      monthSummary(
        [
          row('2026-11-02', 100),
          row('2026-11-04', 20, { charges_unavailable_trades: 1, net_charges_complete: false }),
        ],
        { days_without_sync: ['2026-11-03'], start: '2026-11-02' }
      ),
      '2026-11',
      '2026-11-23'
    )
    const by = (d: string) => cal.days.find((x) => x.date === d)
    // nothing traded and nothing missing
    expect(by('2026-11-05')).toMatchObject({ status: 'none', incomplete: false, net: null })
    // broker trades were not collected: may not be a no-trade day
    expect(by('2026-11-03')).toMatchObject({ status: 'unavailable', incomplete: true, net: null })
    expect(by('2026-11-03')?.note).toMatch(/not collected/)
    // before the tracked history: not a no-trade day, and not counted as missing data
    expect(by('2026-11-01')).toMatchObject({ status: 'untracked', incomplete: false })
    // a traded day whose charges are missing keeps its P&L but is marked
    expect(by('2026-11-04')).toMatchObject({ status: 'profit', incomplete: true })
    expect(by('2026-11-04')?.note).toMatch(/no charge estimate/)
    expect(cal.totals.incompleteDays).toBe(2)
    // an unavailable day is not counted as a trading day
    expect(cal.totals.tradingDays).toBe(2)
  })

  it('never marks a future day unavailable', () => {
    const cal = buildMonthCalendar(
      monthSummary([], { days_without_sync: [] }),
      '2026-11',
      '2026-11-10'
    )
    const future = cal.days.find((d) => d.date === '2026-11-20')
    expect(future).toMatchObject({ future: true, status: 'none', incomplete: false })
  })

  it('lays weeks out Monday first with padding', () => {
    const cal = buildMonthCalendar(monthSummary([]), '2026-11', '2026-11-23')
    // 1 Nov 2026 is a Sunday: six padding cells, then the 1st in the seventh column
    expect(cal.weeks[0].slice(0, 6).every((c) => c === null)).toBe(true)
    expect(cal.weeks[0][6]?.date).toBe('2026-11-01')
    for (const w of cal.weeks) expect(w).toHaveLength(7)
  })
})

describe('month navigation helpers', () => {
  it('shifts across year ends', () => {
    expect(shiftMonth('2026-11', 1)).toBe('2026-12')
    expect(shiftMonth('2026-12', 1)).toBe('2027-01')
    expect(shiftMonth('2026-01', -1)).toBe('2025-12')
    expect(monthTitle('2026-11')).toBe('November 2026')
  })

  it('requests the whole month, or up to today for the current month', () => {
    expect(monthRange('2026-10', '2026-11-23')).toEqual({ start: '2026-10-01', end: '2026-10-31' })
    expect(monthRange('2026-11', '2026-11-23')).toEqual({ start: '2026-11-01', end: '2026-11-23' })
    expect(monthRange('2028-02', '2030-01-01').end).toBe('2028-02-29')
  })

  it('stops at the current month and at the first tracked month', () => {
    expect(canGoNext('2026-10', '2026-11-23')).toBe(true)
    expect(canGoNext('2026-11', '2026-11-23')).toBe(false)
    expect(canGoPrevious('2026-11', '2026-11-01')).toBe(false)
    expect(canGoPrevious('2026-11', '2026-10-05')).toBe(true)
    expect(canGoPrevious('2020-01', undefined)).toBe(true)
  })
})
