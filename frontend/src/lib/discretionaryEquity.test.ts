import { describe, expect, it } from 'vitest'
import {
  breakdownVisibility,
  equityCurveBasis,
  equityCurveReconciliation,
  netEquityFromDailyRows,
  timeOfDayBars,
} from '@/lib/discretionaryPnlView'
import { breakdownRow, summary, tradingBreakdowns } from '@/test/discretionaryFixtures'

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

describe('netEquityFromDailyRows (Equity Curve)', () => {
  const rows = [day('2026-11-18', -40), day('2026-11-16', 100.1), day('2026-11-20', 25.25)]

  it('is the running sum of the daily net rows, oldest first, and ends at the period net', () => {
    const curve = netEquityFromDailyRows(rows)
    expect(curve.points.map((p) => p.closed_at)).toEqual(['2026-11-16', '2026-11-18', '2026-11-20'])
    expect(curve.points.map((p) => p.net)).toEqual([100.1, -40, 25.25])
    expect(curve.points.map((p) => p.equity)).toEqual([100.1, 60.1, 85.35])
    const periodNet = rows.reduce((a, r) => a + r.net_of_charges, 0)
    expect(curve.latestEquity).toBeCloseTo(periodNet, 2)
    expect(equityCurveReconciliation(curve, periodNet, money).kind).toBe('match')
  })

  it('draws no point for a day without a row and counts nothing as zero', () => {
    const curve = netEquityFromDailyRows(rows)
    // 17 and 19 November have no row: they are not points
    expect(curve.points).toHaveLength(3)
    expect(curve.skipped).toBe(0)
  })

  it('reports drawdown from the highest cumulative point, starting at zero', () => {
    const curve = netEquityFromDailyRows([day('2026-11-16', -30), day('2026-11-17', 50)])
    expect(curve.points.map((p) => p.drawdown)).toEqual([30, 0])
    expect(curve.maxDrawdown).toBe(30)
    expect(curve.peakEquity).toBe(20)
  })

  it('counts days whose net lacks charge estimates and says so in the basis', () => {
    const curve = netEquityFromDailyRows([
      day('2026-11-16', 10),
      day('2026-11-17', 10, { net_charges_complete: false, charges_unavailable_trades: 1 }),
    ])
    expect(curve.incompleteDays).toBe(1)
    expect(equityCurveBasis(curve)).toContain('1 day(s) include trades without a charge estimate')
    expect(equityCurveBasis(curve)).toContain('not an account balance')
  })

  it('flags a last point that differs from the period net', () => {
    const curve = netEquityFromDailyRows([day('2026-11-16', 10), day('2026-11-17', 10)])
    const rec = equityCurveReconciliation(curve, 25, money)
    expect(rec.kind).toBe('mismatch')
    expect(rec.text).toContain('Rs 20.00')
    expect(rec.text).toContain('Rs 25.00')
  })

  it('is empty without rows', () => {
    expect(netEquityFromDailyRows(undefined).points).toEqual([])
    expect(netEquityFromDailyRows([]).latestEquity).toBe(0)
  })
})

describe('timeOfDayBars', () => {
  it('keeps the server order, drops empty intervals and labels the trade count', () => {
    const bars = timeOfDayBars([
      breakdownRow('09:15-10:00', { trades: 4, net_of_charges: 120 }),
      breakdownRow('10:00-11:00', { trades: 0, net_of_charges: 0 }),
      breakdownRow('other', { trades: 2, net_of_charges: -60 }),
    ])
    expect(bars.map((b) => b.label)).toEqual(['09:15-10:00 (4)', 'Other (outside NSE hours) (2)'])
    expect(bars.map((b) => b.value)).toEqual([120, -60])
    expect(bars.map((b) => b.pct)).toEqual([100, 50])
  })
})

describe('breakdown visibility thresholds', () => {
  const base = summary({
    metrics: { ...summary().metrics, trades_count: 12 },
    breakdowns: tradingBreakdowns({
      time_of_day: [
        breakdownRow('09:15-10:00', { trades: 6 }),
        breakdownRow('10:00-11:00', { trades: 6 }),
      ],
    }),
  })

  it('shows time of day with two populated intervals', () => {
    expect(breakdownVisibility(base).patterns).toContain('time_of_day')
  })
  it('shows time of day with few trades when two intervals are populated', () => {
    const s = {
      ...base,
      metrics: { ...base.metrics, trades_count: 2 },
      breakdowns: tradingBreakdowns({
        time_of_day: [
          breakdownRow('09:15-10:00', { trades: 1 }),
          breakdownRow('10:00-11:00', { trades: 1 }),
        ],
      }),
    }
    expect(breakdownVisibility(s).patterns).toContain('time_of_day')
  })
  it('hides time of day with one populated interval', () => {
    const s = {
      ...base,
      breakdowns: tradingBreakdowns({
        time_of_day: [
          breakdownRow('09:15-10:00', { trades: 12 }),
          breakdownRow('10:00-11:00', { trades: 0 }),
        ],
      }),
    }
    expect(breakdownVisibility(s).patterns).not.toContain('time_of_day')
  })
  it('shows Long vs Short when both sides have a trade, and not with one side only', () => {
    const ok = summary({
      by_side: [breakdownRow('LONG', { trades: 3 }), breakdownRow('SHORT', { trades: 1 })],
    })
    const oneSide = summary({
      by_side: [breakdownRow('LONG', { trades: 8 }), breakdownRow('SHORT', { trades: 0 })],
    })
    expect(breakdownVisibility(ok).side).toBe(true)
    expect(breakdownVisibility(oneSide).side).toBe(false)
  })
  it('hides symbol and product with a single category', () => {
    const one = summary({ by_symbol: [breakdownRow('INFY')], by_product: [breakdownRow('MIS')] })
    expect(breakdownVisibility(one).symbol).toBe(false)
    expect(breakdownVisibility(one).product).toBe(false)
    const two = summary({
      by_symbol: [breakdownRow('INFY'), breakdownRow('TCS')],
      by_product: [breakdownRow('MIS'), breakdownRow('CNC')],
    })
    expect(breakdownVisibility(two).symbol).toBe(true)
    expect(breakdownVisibility(two).product).toBe(true)
  })
})

describe('Equity Curve last point equals the headline net (real-ledger-like rounding)', () => {
  // Three days the server rounded to 2 decimals; they sum to 2,823.83 while the unrounded period
  // net is 2,823.8226, which the headline shows as 2,823.82.
  const rows = [day('2026-11-18', 186.96), day('2026-11-19', 482.55), day('2026-11-20', 2154.32)]

  it('snaps the last point to the period net when the sum only differs by rounding', () => {
    const curve = netEquityFromDailyRows(rows, [], 2823.8226)
    expect(curve.latestEquity).toBe(2823.82)
    expect(curve.points.map((p) => p.equity)).toEqual([186.96, 669.51, 2823.82])
    expect(curve.peakEquity).toBe(2823.82)
    expect(curve.maxDrawdown).toBe(0)
    expect(money(curve.latestEquity)).toBe(money(Math.round(2823.8226 * 100) / 100))
    expect(equityCurveReconciliation(curve, 2823.8226, money).text).toContain('Rs 2823.82')
  })

  it('keeps the plain running sum when no period net is given', () => {
    expect(netEquityFromDailyRows(rows).latestEquity).toBe(2823.83)
  })

  it('does not hide a real difference: a sum far from the period net is left as it is', () => {
    expect(netEquityFromDailyRows(rows, [], 2900).latestEquity).toBe(2823.83)
  })
})
