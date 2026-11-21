import { describe, expect, it } from 'vitest'
import type { DiscretionarySummary, RiskBlock } from '@/api/discretionaryPnl'
import { consistency, costsBlock, riskBlock, summary, trade } from '@/test/discretionaryFixtures'
import {
  cleanMoney,
  closeDateCount,
  drawdownReconciliation,
  netEquityDaily,
  netEquityPerTrade,
  netEquitySeries,
  periodProfile,
  verdictLine,
  weaknessLine,
} from './discretionaryPnlView'

const money = (v: number) => `Rs ${v.toFixed(2)}`

// ---------------------------------------------------------------- golden numbers
// GOLDEN comes from the REAL backend function, run over exactly these trades:
//   from services.discretionary_analytics import drawdown
//   drawdown([{"id": id, "closed_at": ..., "net_pnl": ...}, ...])
// (services/discretionary_analytics.py, drawdown(); generator kept out of the repo). The numbers
// below are what Python returned; the TypeScript port must reproduce them.
type GoldenTrade = { id: number; closed_at: string; net_pnl: number | null }
const GOLDEN: Record<
  string,
  {
    trades: GoldenTrade[]
    expected: {
      positions: number
      net_unavailable_positions: number
      max_drawdown: number
      current_drawdown: number
      peak_equity: number
      latest_equity: number
    }
  }
> = {
  first_trade_loss: {
    trades: [
      {
        id: 1,
        closed_at: '2026-11-16 09:20:00',
        net_pnl: -50.0,
      },
      {
        id: 2,
        closed_at: '2026-11-16 09:40:00',
        net_pnl: 30.0,
      },
      {
        id: 3,
        closed_at: '2026-11-16 10:05:00',
        net_pnl: -20.0,
      },
      {
        id: 4,
        closed_at: '2026-11-16 10:30:00',
        net_pnl: 80.0,
      },
    ],
    expected: {
      positions: 4,
      net_unavailable_positions: 0,
      max_drawdown: 50.0,
      current_drawdown: 0.0,
      peak_equity: 40.0,
      latest_equity: 40.0,
    },
  },
  same_second_ties: {
    trades: [
      {
        id: 9,
        closed_at: '2026-11-16 10:00:00',
        net_pnl: -40.0,
      },
      {
        id: 3,
        closed_at: '2026-11-16 10:00:00',
        net_pnl: 100.0,
      },
      {
        id: 5,
        closed_at: '2026-11-16 10:00:00',
        net_pnl: -70.5,
      },
      {
        id: 1,
        closed_at: '2026-11-16 09:59:59',
        net_pnl: 25.25,
      },
      {
        id: 12,
        closed_at: '2026-11-17 09:15:00',
        net_pnl: -10.0,
      },
    ],
    expected: {
      positions: 5,
      net_unavailable_positions: 0,
      max_drawdown: 120.5,
      current_drawdown: 120.5,
      peak_equity: 125.25,
      latest_equity: 4.75,
    },
  },
  null_nets_and_noise: {
    trades: [
      {
        id: 1,
        closed_at: '2026-11-16 09:20:00',
        net_pnl: 60.0,
      },
      {
        id: 2,
        closed_at: '2026-11-16 09:30:00',
        net_pnl: null,
      },
      {
        id: 3,
        closed_at: '2026-11-16 09:40:00',
        net_pnl: 0.004,
      },
      {
        id: 4,
        closed_at: '2026-11-16 09:50:00',
        net_pnl: -0.003,
      },
      {
        id: 5,
        closed_at: '2026-11-16 10:00:00',
        net_pnl: -25.5,
      },
      {
        id: 6,
        closed_at: '2026-11-16 10:10:00',
        net_pnl: null,
      },
      {
        id: 7,
        closed_at: '2026-11-16 10:20:00',
        net_pnl: 10.0,
      },
    ],
    expected: {
      positions: 5,
      net_unavailable_positions: 2,
      max_drawdown: 25.5,
      current_drawdown: 15.5,
      peak_equity: 60.0,
      latest_equity: 44.5,
    },
  },
  random_25: {
    trades: [
      {
        id: 1,
        closed_at: '2026-11-16 10:07:00',
        net_pnl: 98.33,
      },
      {
        id: 2,
        closed_at: '2026-11-16 11:14:00',
        net_pnl: -46.81,
      },
      {
        id: 3,
        closed_at: '2026-11-16 12:21:00',
        net_pnl: -13.78,
      },
      {
        id: 4,
        closed_at: '2026-11-16 13:28:00',
        net_pnl: 35.74,
      },
      {
        id: 5,
        closed_at: '2026-11-16 14:35:00',
        net_pnl: -15.87,
      },
      {
        id: 6,
        closed_at: '2026-11-16 09:42:00',
        net_pnl: -79.23,
      },
      {
        id: 7,
        closed_at: '2026-11-16 10:49:00',
        net_pnl: -45.67,
      },
      {
        id: 8,
        closed_at: '2026-11-16 11:56:00',
        net_pnl: -11.3,
      },
      {
        id: 9,
        closed_at: '2026-11-17 12:03:00',
        net_pnl: -47.57,
      },
      {
        id: 10,
        closed_at: '2026-11-17 13:10:00',
        net_pnl: 33.09,
      },
      {
        id: 11,
        closed_at: '2026-11-17 14:17:00',
        net_pnl: null,
      },
      {
        id: 12,
        closed_at: '2026-11-17 09:24:00',
        net_pnl: -5.2,
      },
      {
        id: 13,
        closed_at: '2026-11-17 10:31:00',
        net_pnl: -14.63,
      },
      {
        id: 14,
        closed_at: '2026-11-17 11:38:00',
        net_pnl: 91.13,
      },
      {
        id: 15,
        closed_at: '2026-11-17 12:45:00',
        net_pnl: -33.49,
      },
      {
        id: 16,
        closed_at: '2026-11-17 13:52:00',
        net_pnl: 94.43,
      },
      {
        id: 17,
        closed_at: '2026-11-18 14:59:00',
        net_pnl: 9.08,
      },
      {
        id: 18,
        closed_at: '2026-11-18 09:06:00',
        net_pnl: -17.66,
      },
      {
        id: 19,
        closed_at: '2026-11-18 10:13:00',
        net_pnl: -62.53,
      },
      {
        id: 20,
        closed_at: '2026-11-18 11:20:00',
        net_pnl: 49.03,
      },
      {
        id: 21,
        closed_at: '2026-11-18 12:27:00',
        net_pnl: -9.61,
      },
      {
        id: 22,
        closed_at: '2026-11-18 13:34:00',
        net_pnl: null,
      },
      {
        id: 23,
        closed_at: '2026-11-18 14:41:00',
        net_pnl: 92.85,
      },
      {
        id: 24,
        closed_at: '2026-11-18 09:48:00',
        net_pnl: 18.98,
      },
      {
        id: 25,
        closed_at: '2026-11-19 10:55:00',
        net_pnl: 83.79,
      },
    ],
    expected: {
      positions: 23,
      net_unavailable_positions: 2,
      max_drawdown: 117.56,
      current_drawdown: 0.0,
      peak_equity: 203.1,
      latest_equity: 203.1,
    },
  },
}

function asTrades(rows: GoldenTrade[]) {
  return rows.map((r) => ({ ...r, symbol: 'X', direction: 'LONG' as const }))
}

/** Plain-loop reference of the backend rule, independent of the implementation under test. */
function referenceDrawdown(rows: GoldenTrade[]) {
  const sorted = rows.slice()
  // (str(closed_at), id) order
  for (let i = 1; i < sorted.length; i++) {
    let j = i
    while (
      j > 0 &&
      (sorted[j - 1].closed_at > sorted[j].closed_at ||
        (sorted[j - 1].closed_at === sorted[j].closed_at && sorted[j - 1].id > sorted[j].id))
    ) {
      const t = sorted[j]
      sorted[j] = sorted[j - 1]
      sorted[j - 1] = t
      j--
    }
  }
  let equity = 0
  let peak = 0
  let worst = 0
  let skipped = 0
  for (const r of sorted) {
    if (r.net_pnl === null) {
      skipped++
      continue
    }
    const pnl = Math.abs(r.net_pnl) < 0.005 ? 0 : r.net_pnl
    equity += pnl
    if (equity > peak) peak = equity
    if (peak - equity > worst) worst = peak - equity
  }
  return { max: worst, current: peak - equity, peak, latest: equity, skipped }
}

describe('netEquityPerTrade against the real backend function (golden numbers)', () => {
  for (const [name, fx] of Object.entries(GOLDEN)) {
    it(`reproduces drawdown() for ${name}`, () => {
      const c = netEquityPerTrade(asTrades(fx.trades))
      const e = fx.expected
      expect(c.used).toBe(e.positions)
      expect(c.skipped).toBe(e.net_unavailable_positions)
      expect(c.maxDrawdown).toBeCloseTo(e.max_drawdown, 2)
      expect(c.currentDrawdown).toBeCloseTo(e.current_drawdown, 2)
      expect(c.peakEquity).toBeCloseTo(e.peak_equity, 2)
      expect(c.latestEquity).toBeCloseTo(e.latest_equity, 2)
    })
  }

  it('starts the peak at 0, so a first-trade loss is already a drawdown', () => {
    const c = netEquityPerTrade(asTrades(GOLDEN.first_trade_loss.trades))
    expect(c.points[0]).toMatchObject({ index: 1, equity: -50, peak: 0, drawdown: 50 })
    expect(c.maxDrawdownAt).toBe('2026-11-16 09:20:00')
  })

  it('orders same-second closes by id, as the server does', () => {
    const c = netEquityPerTrade(asTrades(GOLDEN.same_second_ties.trades))
    expect(c.points.map((p) => p.id)).toEqual([1, 3, 5, 9, 12])
    expect(c.points.map((p) => p.index)).toEqual([1, 2, 3, 4, 5])
  })

  it('skips a position without a net (never zero) and cleans sub-paise noise', () => {
    const c = netEquityPerTrade(asTrades(GOLDEN.null_nets_and_noise.trades))
    expect(c.skipped).toBe(2)
    expect(c.points).toHaveLength(5)
    expect(c.points[1].net).toBe(0)
    expect(c.points[2].net).toBe(0)
    expect(cleanMoney(0.004)).toBe(0)
    expect(cleanMoney(-0.005)).toBe(-0.005)
  })

  it('matches a plain-loop reference on 200 random sequences', () => {
    let seed = 7
    const rnd = () => {
      seed = (seed * 1664525 + 1013904223) % 4294967296
      return seed / 4294967296
    }
    for (let run = 0; run < 200; run++) {
      const n = 1 + Math.floor(rnd() * 40)
      const rows: GoldenTrade[] = []
      for (let i = 0; i < n; i++) {
        const r = rnd()
        rows.push({
          id: 1 + Math.floor(rnd() * 1000) + i * 1000,
          // a handful of distinct close seconds so ties happen
          closed_at: `2026-11-1${6 + Math.floor(rnd() * 3)} 10:0${Math.floor(rnd() * 4)}:00`,
          net_pnl:
            r < 0.1
              ? null
              : r < 0.2
                ? (rnd() - 0.5) * 0.01
                : Math.round((rnd() * 200 - 90) * 100) / 100,
        })
      }
      const ref = referenceDrawdown(rows)
      const c = netEquityPerTrade(asTrades(rows))
      expect(c.maxDrawdown).toBeCloseTo(ref.max, 9)
      expect(c.currentDrawdown).toBeCloseTo(ref.current, 9)
      expect(c.peakEquity).toBeCloseTo(ref.peak, 9)
      expect(c.latestEquity).toBeCloseTo(ref.latest, 9)
      expect(c.skipped).toBe(ref.skipped)
    }
  })

  it('has no points for no trades and does not touch the end-of-day series', () => {
    expect(netEquityPerTrade([]).points).toEqual([])
    const day = netEquitySeries([
      { id: 1, closed_at: '2026-11-16 10:00:00', trade_date: '2026-11-16', net_pnl: 10 },
      { id: 2, closed_at: '2026-11-17 10:00:00', trade_date: '2026-11-17', net_pnl: -4 },
    ])
    expect(day.points).toEqual([
      { date: '2026-11-16', value: 10 },
      { date: '2026-11-17', value: 6 },
    ])
    const d = netEquityDaily(day.points)
    expect(d.maxDrawdown).toBe(4)
    expect(d.currentDrawdown).toBe(4)
  })
})

describe('drawdownReconciliation', () => {
  const trades = asTrades(GOLDEN.null_nets_and_noise.trades)
  const curve = netEquityPerTrade(trades)
  const risk = (extra: Partial<RiskBlock> = {}): RiskBlock =>
    riskBlock({
      positions: 5,
      net_unavailable_positions: 2,
      max_drawdown: 25.5,
      current_drawdown: 15.5,
      peak_equity: 60,
      latest_equity: 44.5,
      ...extra,
    })
  const base = {
    curve,
    mode: 'position' as const,
    tradesShown: 7,
    tradesTotal: 7,
    formatCurrency: money,
  }

  it('says the maximum equals the Risk card when nothing is cut and nets are left out the same way', () => {
    const r = drawdownReconciliation({ ...base, risk: risk() })
    expect(r.kind).toBe('match')
    expect(r.text).toContain('Max drawdown on this curve equals the Risk card')
    expect(r.text).toContain('2 position(s) without a net P&L are left out of both')
  })

  it('matches on every golden fixture against the golden Risk figures', () => {
    for (const fx of Object.values(GOLDEN)) {
      const c = netEquityPerTrade(asTrades(fx.trades))
      const e = fx.expected
      const r = drawdownReconciliation({
        curve: c,
        mode: 'position',
        tradesShown: fx.trades.length,
        tradesTotal: fx.trades.length,
        formatCurrency: money,
        risk: riskBlock({
          positions: e.positions,
          net_unavailable_positions: e.net_unavailable_positions,
          max_drawdown: e.max_drawdown,
          current_drawdown: e.current_drawdown,
          peak_equity: e.peak_equity,
          latest_equity: e.latest_equity,
        }),
      })
      expect(r.kind).toBe('match')
    }
  })

  it('names the cut list and the basis of each when the trades are truncated', () => {
    const r = drawdownReconciliation({ ...base, tradesShown: 7, tradesTotal: 640, risk: risk() })
    expect(r.kind).toBe('truncated')
    expect(r.text).toContain('Curve shows the newest 7 of 640 closed positions')
    expect(r.text).toContain('the Risk card covers all 640')
  })

  it('says exactly what differs when the position sets differ', () => {
    const r = drawdownReconciliation({
      ...base,
      risk: risk({ max_drawdown: 99, positions: 6, net_unavailable_positions: 1 }),
    })
    expect(r.kind).toBe('mismatch')
    expect(r.text).toContain('Not reconciled')
    expect(r.text).toContain('uses 5 position(s) with a net P&L (2 left out)')
    expect(r.text).toContain('Rs 99.00')
  })

  it('describes the end-of-day curve and why its drawdown can be smaller', () => {
    const day = netEquityDaily([
      { date: '2026-11-16', value: 10 },
      { date: '2026-11-17', value: 6 },
    ])
    const r = drawdownReconciliation({
      curve: day,
      mode: 'day',
      tradesShown: 2,
      tradesTotal: 2,
      formatCurrency: money,
      risk: risk({ max_drawdown: 40 }),
    })
    expect(r.kind).toBe('day_curve')
    expect(r.text).toContain('end-of-day')
    expect(r.text).toContain('can be smaller than the per-position Risk card figure (Rs 40.00)')
  })

  it('does not invent a comparison when the server sent no Risk card', () => {
    const r = drawdownReconciliation({ ...base, risk: null })
    expect(r.kind).toBe('no_risk')
    expect(r.text).toContain('no Risk card')
  })
})

describe('periodProfile', () => {
  const cases: [string, number, number, string, Record<string, unknown>][] = [
    ['30d', 0, 0, 'session', { showMonthly: false, showByPeriodTabs: false, showDailyBars: false }],
    [
      '30d',
      1,
      1,
      'session',
      { showWeekday: false, collapseSecondary: true, perPositionCurve: true },
    ],
    ['30d', 2, 1, 'short', { showMonthly: false, showWeekday: true, byPeriodTabs: ['daily'] }],
    ['7d', 7, 1, 'short', { showDailyBars: true, collapseSecondary: true }],
    [
      '30d',
      8,
      1,
      'medium',
      { showMonthly: false, byPeriodTabs: ['daily', 'weekly'], showWeekday: true },
    ],
    ['30d', 8, 2, 'medium', { showMonthly: true, byPeriodTabs: ['daily', 'weekly', 'monthly'] }],
    ['30d', 31, 2, 'medium', { collapseSecondary: true, perPositionCurve: true }],
    [
      'all',
      32,
      2,
      'long',
      { collapseSecondary: false, perPositionCurve: false, showMonthly: true },
    ],
    ['all', 5, 3, 'long', { showMonthly: true, showByPeriodTabs: true, showDailyBars: true }],
    ['1d', 3, 1, 'session', { showWeekday: false, showByPeriodTabs: false }],
  ]
  for (const [period, closeDates, months, profile, expected] of cases) {
    it(`${period}, ${closeDates} date(s), ${months} month(s) -> ${profile}`, () => {
      const p = periodProfile({ period, closeDates, months })
      expect(p.profile).toBe(profile)
      expect(p).toMatchObject(expected)
    })
  }

  it('always says what is hidden, except in the long profile', () => {
    expect(periodProfile({ period: '1d', closeDates: 1, months: 1 }).note).toContain(
      'one trading day'
    )
    expect(periodProfile({ period: '30d', closeDates: 3, months: 1 }).note).toContain(
      '3 trading days'
    )
    expect(periodProfile({ period: 'all', closeDates: 40, months: 4 }).note).toBeNull()
  })

  it('counts close dates from the server daily rows, else from the trade dates', () => {
    const s = summary()
    expect(closeDateCount(s)).toBe(1)
    const noDaily = { ...s, by_period: { ...s.by_period, daily: [] } }
    expect(closeDateCount(noDaily)).toBe(1)
    const two = { ...noDaily, trades: [trade(), trade({ id: 2, trade_date: '2026-11-17' })] }
    expect(closeDateCount(two)).toBe(2)
    expect(closeDateCount(undefined)).toBe(0)
  })
})

describe('verdictLine and weaknessLine', () => {
  const s = (extra: Partial<DiscretionarySummary> = {}) => summary(extra)

  it('says profitable, not profitable or break-even on net, with count and win rate', () => {
    expect(verdictLine(s({ realized_net_of_charges: 120 }), money).text).toContain(
      'Profitable on net: Rs 120.00'
    )
    const loss = verdictLine(s({ realized_net_of_charges: -80 }), money)
    expect(loss.tone).toBe('loss')
    expect(loss.text).toContain('Not profitable on net: Rs 80.00 lost')
    expect(loss.text).toContain('2 completed trades')
    expect(loss.text).toContain('Win rate 50.0%')
    expect(verdictLine(s({ realized_net_of_charges: 0.001 }), money).tone).toBe('flat')
  })

  it('has no verdict sentence of value with no closed position', () => {
    const e = s()
    e.metrics = { ...e.metrics, trades_count: 0 }
    expect(verdictLine(e, money).tone).toBe('none')
    expect(weaknessLine(e, money)).toBeNull()
  })

  it('picks the single biggest weakness in a fixed order, from existing fields only', () => {
    // 1. charges turn a gross profit into a net loss
    expect(weaknessLine(s({ realized_gross: 50, realized_net_of_charges: -10 }), money)).toContain(
      'Charges turned a gross profit of Rs 50.00 into a net loss'
    )
    // 2. charges take 30% or more of gross profit
    expect(
      weaknessLine(s({ costs: costsBlock({ charges_pct_of_gross_profit: 42.5 }) }), money)
    ).toBe('Charges take 42.5% of gross profit.')
    // 3. largest loss bigger than largest win
    const c = consistency()
    const big = s({
      costs: costsBlock({ charges_pct_of_gross_profit: 5 }),
      consistency: {
        ...c,
        largest_win: { ...c.largest_win!, net_pnl: 100 },
        largest_loss: { ...c.largest_loss!, net_pnl: -250 },
      },
    })
    expect(weaknessLine(big, money)).toBe(
      'Largest loss (Rs 250.00) is bigger than the largest win (Rs 100.00).'
    )
    // 4. a current drawdown
    expect(
      weaknessLine(
        s({ realized_gross: 10, risk: riskBlock({ current_drawdown: 75 }), consistency: null }),
        money
      )
    ).toBe('Currently Rs 75.00 below the best net equity so far.')
    // 5. low sample
    const low = weaknessLine(
      s({
        realized_gross: 10,
        consistency: null,
        risk: riskBlock({ current_drawdown: 0 }),
        sample: {
          completed_trades: 2,
          trading_days: 1,
          min_trades: 30,
          min_days: 10,
          sufficient: false,
        },
      }),
      money
    )
    expect(low).toContain('Low sample: 2 completed trade(s) over 1 trading day(s)')
    // none
    expect(
      weaknessLine(
        s({ realized_gross: 10, consistency: null, risk: riskBlock({ current_drawdown: 0 }) }),
        money
      )
    ).toBeNull()
  })

  it('never uses advice or AI wording', () => {
    const text = [
      verdictLine(s({ realized_net_of_charges: -80 }), money).text,
      weaknessLine(s({ realized_gross: 50, realized_net_of_charges: -10 }), money) ?? '',
    ].join(' ')
    expect(text).not.toMatch(/should|recommend|AI|paper/i)
  })
})
