import { describe, expect, it } from 'vitest'
import {
  dataQuality,
  emptySummary,
  emptyToday,
  position,
  summary,
} from '@/test/discretionaryFixtures'
import * as view from './discretionaryPnlView'
import {
  actionableWarnings,
  awaitingAttribution,
  awaitingAttributionText,
  chargesBadge,
  distinctOptions,
  filterChoices,
  fmtAge,
  fmtClock,
  fmtProfitFactor,
  fmtWinRate,
  holdingRows,
  isEmptyBook,
  netDefinitionText,
  netIncomplete,
  parseSourceParam,
  pendingAttributionLines,
  priceBadge,
  reasonText,
  resolveSource,
  summarizeUnrealized,
  syncSummary,
  todayUnrealized,
  totalCard,
  totalPnl,
} from './discretionaryPnlView'

describe('source parameter', () => {
  it('treats absent, unknown and strategies as strategies', () => {
    expect(parseSourceParam(null)).toBe('strategies')
    expect(parseSourceParam(undefined)).toBe('strategies')
    expect(parseSourceParam('strategies')).toBe('strategies')
    expect(parseSourceParam('Discretionary')).toBe('strategies')
    expect(parseSourceParam('discretionary')).toBe('discretionary')
  })

  it('shows discretionary only when the backend says enabled', () => {
    expect(resolveSource('discretionary', 'enabled')).toBe('discretionary')
    expect(resolveSource('discretionary', 'disabled')).toBe('strategies')
    expect(resolveSource('discretionary', 'pending')).toBe('pending')
  })

  it('never waits or switches when the URL did not ask for discretionary', () => {
    for (const availability of ['pending', 'enabled', 'disabled'] as const) {
      expect(resolveSource(null, availability)).toBe('strategies')
      expect(resolveSource('strategies', availability)).toBe('strategies')
    }
  })
})

describe('price badges', () => {
  it('maps every price status', () => {
    expect(priceBadge('live', 4)).toMatchObject({ label: 'Live 4s', tone: 'ok' })
    expect(priceBadge('stale', 150)).toMatchObject({ label: 'Stale 2m', tone: 'warn' })
    expect(priceBadge('last_close')).toMatchObject({ label: 'Last close', tone: 'warn' })
    expect(priceBadge('cached', 3700)).toMatchObject({ label: 'Cached 1h 01m', tone: 'warn' })
    expect(priceBadge('unavailable')).toMatchObject({ label: 'No price', tone: 'bad' })
    expect(priceBadge('not_requested')).toMatchObject({ label: 'Not requested', tone: 'muted' })
    expect(priceBadge('???')).toMatchObject({ label: 'Unknown' })
  })

  it('formats an age and leaves it out when unknown', () => {
    expect(fmtAge(null)).toBe('')
    expect(fmtAge(-1)).toBe('')
    expect(fmtAge(59)).toBe('59s')
    expect(fmtAge(60)).toBe('1m')
    expect(priceBadge('cached', null).label).toBe('Cached')
  })
})

describe('charges badge', () => {
  it('says estimated when charges exist and unavailable when they do not', () => {
    expect(chargesBadge({ charges_status: 'ok' }).label).toBe('estimated')
    expect(chargesBadge({ charges_status: 'unavailable' })).toMatchObject({
      label: 'charges unavailable',
      tone: 'warn',
    })
  })
})

describe('formatters', () => {
  it('shows win rate as a percent and profit factor honestly', () => {
    expect(fmtWinRate(62.5)).toBe('62.5%')
    expect(fmtWinRate(null)).toBe('—')
    expect(fmtProfitFactor(2.456, 3, 1)).toBe('2.46')
    expect(fmtProfitFactor(null, 3, 0)).toBe('∞')
    expect(fmtProfitFactor(null, 0, 0)).toBe('—')
    expect(fmtProfitFactor(0, 0, 2)).toBe('0.00')
  })

  it('shows an unknown net-basis money figure as a dash, never as zero', () => {
    const money = (v: number) => `₹${v.toFixed(2)}`
    expect(view.fmtNetMoney(null, money)).toBe('—')
    expect(view.fmtNetMoney(undefined, money)).toBe('—')
    expect(view.fmtNetMoney(Number.NaN, money)).toBe('—')
    expect(view.fmtNetMoney(0, money)).toBe('₹0.00') // a real zero stays a number
    expect(view.fmtNetMoney(-12.5, money)).toBe('₹-12.50')
    expect(fmtWinRate(null)).toBe('—')
    expect(fmtProfitFactor(null, 0, 0)).toBe('—') // no wins and no losses: nothing to show
  })
})

describe('unrealized', () => {
  it('is none without open positions', () => {
    expect(summarizeUnrealized(emptySummary())).toMatchObject({ kind: 'none', badge: null })
  })

  it('is not requested by default, with its badge', () => {
    const u = summarizeUnrealized(summary())
    expect(u.kind).toBe('not_requested')
    expect(u.badge?.label).toBe('Not requested')
    expect(totalPnl(47.5, u)).toEqual({ value: 47.5, includesUnrealized: false })
  })

  it('is complete when every position is priced, and the total adds it', () => {
    const s = summary({
      quotes_requested: true,
      unrealized_total: 250,
      positions: [position({ price_status: 'live', age_s: 3, ltp: 3050, unrealized: 250 })],
    })
    const u = summarizeUnrealized(s)
    expect(u).toMatchObject({ kind: 'complete', value: 250 })
    expect(u.badge?.label).toBe('Live 3s')
    expect(totalPnl(47.5, u)).toEqual({ value: 297.5, includesUnrealized: true })
  })

  it('is partial when a position has no price, and the total leaves it out', () => {
    const s = summary({
      quotes_requested: true,
      unrealized_total: 250,
      open_positions: 2,
      positions: [
        position({ price_status: 'stale', age_s: 400, ltp: 3050, unrealized: 250 }),
        position({ symbol: 'ABC', price_status: 'unavailable' }),
      ],
    })
    const u = summarizeUnrealized(s)
    expect(u).toMatchObject({ kind: 'partial', unpriced: 1 })
    expect(u.badge?.label).toBe('No price')
    expect(totalPnl(47.5, u).includesUnrealized).toBe(false)
  })
})

describe('warnings and sync line', () => {
  it('drops lines the cards and the sync line already show', () => {
    expect(actionableWarnings(summary().warnings)).toEqual([
      expect.stringContaining('no charge estimate'),
    ])
    expect(actionableWarnings(undefined)).toEqual([])
  })

  it('flags never-synced and missing days', () => {
    expect(syncSummary(undefined).problem).toBe(true)
    expect(
      syncSummary({ last_successful_sync: null, runs_by_day: {}, days_without_sync: [] }).problem
    ).toBe(true)
    expect(
      syncSummary({
        last_successful_sync: '2026-11-17 09:30',
        runs_by_day: {},
        days_without_sync: ['2026-11-12', '2026-11-13'],
      })
    ).toEqual({
      text: 'Ledger sync: last successful 2026-11-17 09:30 IST; no sync on 2026-11-12, 2026-11-13',
      problem: true,
    })
    expect(syncSummary(summary().sync_health)).toEqual({
      text: 'Ledger sync: last successful 2026-11-17 09:30 IST',
      problem: false,
    })
  })
})

describe('book helpers', () => {
  it('knows an empty book', () => {
    expect(isEmptyBook(emptySummary())).toBe(true)
    expect(isEmptyBook(summary())).toBe(false)
    expect(isEmptyBook(undefined)).toBe(true)
    // a partial exit of a position completed after the period: money, no trade, no position
    const legOnly = {
      ...emptySummary(),
      by_period: { ...emptySummary().by_period, daily: summary().by_period.daily },
    }
    expect(isEmptyBook(legOnly)).toBe(false)
  })

  it('reports executions awaiting attribution only for an empty book with unresolved fills', () => {
    const waiting = { ...emptySummary(), executions_collected: 5, executions_unresolved: 5 }
    expect(awaitingAttribution(waiting)).toEqual({ collected: 5, unresolved: 5 })
    expect(
      awaitingAttribution({ ...emptySummary(), executions_collected: 7, executions_unresolved: 2 })
    ).toEqual({ collected: 7, unresolved: 2 })
    // no executions, or none awaiting attribution, or an older server without the counts
    expect(
      awaitingAttribution({ ...emptySummary(), executions_collected: 0, executions_unresolved: 0 })
    ).toBeNull()
    expect(
      awaitingAttribution({ ...emptySummary(), executions_collected: 3, executions_unresolved: 0 })
    ).toBeNull()
    expect(awaitingAttribution(emptySummary())).toBeNull()
    expect(awaitingAttribution(undefined)).toBeNull()
    // trades or positions exist: no empty state at all
    expect(
      awaitingAttribution({ ...summary(), executions_collected: 9, executions_unresolved: 4 })
    ).toBeNull()
  })

  it('words the awaiting-attribution message exactly', () => {
    expect(awaitingAttributionText(5, 5)).toBe(
      'No attributable trades available for performance analysis yet. 5 executions collected; 5 awaiting attribution. Their P&L is excluded until classification is resolved.'
    )
  })

  it('builds filter choices from the data, sorted and distinct', () => {
    expect(distinctOptions(['b', 'a', 'b', '', null, undefined])).toEqual(['a', 'b'])
    const c = filterChoices(summary())
    expect(c.brokers).toEqual(['flattrade', 'zerodha'])
    expect(c.accounts).toEqual(['FT1', 'ZR1'])
    expect(filterChoices(undefined)).toEqual({ brokers: [], accounts: [] })
  })

  it('names the holding buckets', () => {
    expect(holdingRows(summary().by_holding).map((r) => r.key)).toEqual(['intraday', 'overnight'])
  })
})

describe('plain-text labels', () => {
  it('maps raw reason codes to plain words and leaves unknown codes readable', () => {
    expect(reasonText('ambiguous_attribution')).toMatch(/Could not tell whether a strategy or you/)
    expect(reasonText('opening_unknown')).toMatch(/not in the ledger/)
    expect(reasonText('unresolved')).toMatch(/left out of P&L/)
    expect(reasonText('some_new_code')).toBe('some new code')
    expect(reasonText(undefined)).toBe('—')
    expect(reasonText('ambiguous_attribution')).not.toContain('_')
  })

  it('replaces the field names of net_definition with a plain sentence', () => {
    const text = netDefinitionText('net and realized_net_of_charges = realized_gross minus x')
    expect(text).not.toContain('_')
    expect(text).toMatch(/^Net = gross realized minus estimated charges/)
    expect(text).toContain('measured on net P&L')
    expect(text).not.toContain('gross metrics')
    expect(netDefinitionText(undefined)).toBe('')
  })
})

describe('incomplete net', () => {
  it('flags either an explicit incomplete flag or unavailable charges', () => {
    expect(netIncomplete({ net_charges_complete: false })).toBe(true)
    expect(netIncomplete({ charges_unavailable_trades: 2 })).toBe(true)
    expect(netIncomplete({ charges_unavailable_trades: 0, net_charges_complete: true })).toBe(false)
    expect(netIncomplete({})).toBe(false)
  })
})

describe('total card', () => {
  const unrealized = (kind: 'partial' | 'complete' | 'none' | 'not_requested', unpriced = 0) => ({
    kind,
    value: 250,
    unpriced,
    badge: null,
  })

  it('shows the partial sum with a flag instead of saying unrealized is not included', () => {
    const card = totalCard(47.5, unrealized('partial', 2), 0)
    expect(card.value).toBe(297.5)
    expect(card.label).toBe('Net realized + unrealized (partial: 2 positions unpriced)')
    expect(card.incomplete).toBe(true)
    expect(`${card.label} ${card.hint}`).not.toMatch(/not included/)
  })

  it('flags an incomplete net on every kind', () => {
    for (const kind of ['partial', 'complete', 'none', 'not_requested'] as const) {
      const card = totalCard(10, unrealized(kind, 1), 3)
      expect(card.incomplete).toBe(true)
      expect(card.hint).toContain('net incomplete: 3 trade(s) without a charge estimate')
    }
  })

  it('is complete when prices and charges are both known', () => {
    const card = totalCard(10, unrealized('complete'), 0)
    expect(card).toMatchObject({ label: 'Total', value: 260, incomplete: false })
  })

  it('without requested prices shows net realized only and says why', () => {
    const card = totalCard(10, unrealized('not_requested', 1), 0)
    expect(card.value).toBe(10)
    expect(card.hint).toBe('net realized only: live prices not requested')
  })
})

describe('today helpers', () => {
  it('formats the clock of a backend stamp', () => {
    expect(fmtClock('2026-11-17 11:20:00')).toBe('11:20')
    expect(fmtClock(null)).toBe('—')
  })

  it('summarizes today unrealized by state', () => {
    expect(todayUnrealized(emptyToday()).kind).toBe('none')
    const notRequested = todayUnrealized(emptyToday({ open_positions: 2 }))
    expect(notRequested.kind).toBe('not_requested')
    expect(notRequested.badge?.label).toBe('Not requested')
    const live = todayUnrealized(
      emptyToday({
        open_positions: 2,
        quotes_requested: true,
        unrealized: 50,
        price_statuses: { live: 1, stale: 1 },
      })
    )
    expect(live.kind).toBe('complete')
    expect(live.value).toBe(50)
    expect(live.badge?.label).toBe('Stale')
    const partial = todayUnrealized(
      emptyToday({
        open_positions: 2,
        quotes_requested: true,
        unrealized: 50,
        unrealized_unavailable_positions: 1,
        price_statuses: { live: 1, unavailable: 1 },
      })
    )
    expect(partial.kind).toBe('partial')
    expect(partial.badge?.label).toBe('No price')
  })

  it('writes no pending line when nothing is pending', () => {
    expect(pendingAttributionLines(undefined)).toEqual([])
    expect(pendingAttributionLines(emptyToday().pending_attribution)).toEqual([])
  })

  it('gives the grace count, the earliest time and the other-reason count', () => {
    const lines = pendingAttributionLines({
      ...emptyToday().pending_attribution,
      grace_count: 1,
      earliest_attributable_at: '2026-11-17 11:20:00',
      grace_fills: [
        {
          id: 1,
          broker: 'flattrade',
          account: 'A1',
          symbol: 'SBIN',
          exchange: 'NSE',
          product: 'MIS',
          side: 'SELL',
          qty: 45,
          price: 105,
          fill_time: '2026-11-17 10:50:00',
          attributable_from: '2026-11-17 11:20:00',
          due: false,
          affects_open_position: false,
        },
      ],
      other_today: 2,
    })
    expect(lines).toEqual([
      '1 execution(s) awaiting attribution: they are inside the 30-minute safety window that lets a strategy order tag arrive, and are not counted in P&L yet. Earliest attribution at 11:20 IST, at the first ledger sync after it.',
      '2 execution(s) of today stay unresolved for another reason and are excluded from P&L; see the unresolved fills warning.',
    ])
  })
})

describe('dashboard additions', () => {
  it('parses naive IST stamps whatever the browser time zone', () => {
    expect(view.parseIstStamp('2026-11-17 09:15:00')).toBe(Date.UTC(2026, 10, 17, 3, 45, 0))
    expect(view.parseIstStamp('2026-11-17T09:15')).toBe(Date.UTC(2026, 10, 17, 3, 45, 0))
    expect(view.parseIstStamp('garbage')).toBeNull()
    expect(view.parseIstStamp(null)).toBeNull()
  })

  it('formats a position age as minutes, hours and minutes, or days and hours', () => {
    const now = Date.UTC(2026, 10, 17, 5, 45, 0) // 11:15 IST
    expect(view.fmtPositionAge('2026-11-17 10:30:00', now)).toBe('45m')
    expect(view.fmtPositionAge('2026-11-17 09:00:00', now)).toBe('2h 15m')
    expect(view.fmtPositionAge('2026-11-16 08:15:00', now)).toBe('1d 3h')
    expect(view.fmtPositionAge('2026-11-17 12:00:00', now)).toBe('—') // in the future
    expect(view.fmtPositionAge(undefined, now)).toBe('—')
  })

  it('builds daily bars scaled to the largest day and counts the days', () => {
    const base = summary().by_period.daily[0]
    const rows = [
      base,
      {
        ...base,
        period: '2026-11-17',
        realized_gross: -120,
        net_charges_complete: true,
        charges_unavailable_trades: 0,
      },
      {
        ...base,
        period: '2026-11-18',
        realized_gross: 0,
        net_charges_complete: true,
        charges_unavailable_trades: 0,
      },
    ]
    const out = view.dailyBars(rows)
    expect(out.bars.map((b) => b.pct)).toEqual([50, 100, 0])
    expect(out.bars[0].netComplete).toBe(false)
    expect([out.profitable, out.losing, out.flat]).toEqual([1, 1, 1])
    expect(view.dailyBars(undefined).bars).toEqual([])
  })

  it('shows a row profit factor only when the sample rule allows it', () => {
    const rule = { min_trades: 30, min_days: 10 }
    expect(
      view.rowProfitFactor({ trades: 0, profit_factor: 0, wins: 0, losses: 0 }, rule).value
    ).toBe('—')
    const low = view.rowProfitFactor(
      { trades: 3, profit_factor: 2, wins: 2, losses: 1, sample_ok: false },
      rule
    )
    // below the sample rule: the number is still shown, flagged low with the rule it misses
    expect(low.value).toBe('2.00')
    expect(low.low).toBe(true)
    expect(low.title).toBe(
      'Low sample: indicative only; needs at least 30 completed trades over 10 trading days (this group has 3)'
    )
    expect(
      view.rowProfitFactor(
        { trades: 40, profit_factor: 2, wins: 25, losses: 15, sample_ok: true },
        rule
      ).value
    ).toBe('2.00')
    expect(
      view.rowProfitFactor({
        trades: 40,
        profit_factor: null,
        wins: 40,
        losses: 0,
        sample_ok: true,
      }).value
    ).toBe('∞')
  })

  it('names the sample behind the ratios and flags a low sample', () => {
    expect(view.sampleNote(undefined)).toBeNull()
    const low = view.sampleNote({
      completed_trades: 1,
      trading_days: 1,
      min_trades: 30,
      min_days: 10,
      sufficient: false,
    })
    expect(low?.low).toBe(true)
    expect(low?.text).toMatch(
      /^Ratios based on 1 completed trade\(s\) over 1 trading day\(s\)\. Low sample/
    )
  })

  it('lists the data-quality states, adding Not placed only when there is one', () => {
    const states = view.dataQualityStates(dataQuality())
    expect(states.map((s) => [s.key, s.count])).toEqual([
      ['collected', 9],
      ['awaiting', 2],
      ['open', 3],
      ['included', 3],
      ['not_placed', 1],
    ])
    const dq = dataQuality({ not_placed: 0 })
    expect(view.dataQualityStates(dq).map((s) => s.key)).not.toContain('not_placed')
    const sum = states.slice(1).reduce((acc, s) => acc + s.count, 0)
    expect(sum).toBe(states[0].count)
  })

  it('describes prices only when they were requested', () => {
    expect(view.dataQualityPrices(dataQuality())).toBe('Prices not requested')
    const p = { requested: true, open_positions: 2, statuses: { live: 2 }, missing: 0, stale: 0 }
    expect(view.dataQualityPrices(dataQuality({ prices: p }))).toBe('Prices: all live')
    expect(view.dataQualityPrices(dataQuality({ prices: { ...p, missing: 1, stale: 1 } }))).toBe(
      'Prices: 1 without a usable price, 1 stale, last close or cached'
    )
    expect(view.dataQualityPrices(dataQuality({ prices: { ...p, open_positions: 0 } }))).toBe(
      'No open positions to price'
    )
  })

  it('labels the buckets in plain words', () => {
    expect(view.bucketLabel('Mon')).toBe('Monday')
    expect(view.bucketLabel('>4h same day')).toBe('Over 4 h, same day')
    expect(view.bucketLabel('09:15-10:00')).toBe('09:15-10:00')
    expect(view.HOW_YOU_TRADE_OPTIONS.map((o) => o.label)).toEqual([
      'Time of day',
      'Weekday',
      'Holding time',
    ])
  })
})

describe('net equity series', () => {
  const t = (id: number, closed_at: string, net_pnl: number | null) => ({
    id,
    closed_at,
    trade_date: closed_at.slice(0, 10),
    net_pnl,
  })

  it('accumulates net P&L in close order and keeps one end-of-day point per date', () => {
    // given out of order: the series follows closed_at, then id
    const out = view.netEquitySeries([
      t(3, '2026-11-17 10:00:00', -20),
      t(1, '2026-11-16 09:30:00', 100),
      t(2, '2026-11-16 14:00:00', -30),
    ])
    expect(out.points).toEqual([
      { date: '2026-11-16', value: 70 },
      { date: '2026-11-17', value: 50 },
    ])
    expect(out.used).toBe(3)
    expect(out.skipped).toBe(0)
  })

  it('skips a trade without a net instead of counting it as zero, and counts it', () => {
    const out = view.netEquitySeries([
      t(1, '2026-11-16 09:30:00', 50.5),
      t(2, '2026-11-16 10:30:00', null),
      t(3, '2026-11-17 10:30:00', 10.25),
    ])
    expect(out.points).toEqual([
      { date: '2026-11-16', value: 50.5 },
      { date: '2026-11-17', value: 60.75 },
    ])
    expect(out.used).toBe(2)
    expect(out.skipped).toBe(1)
  })

  it('is empty when no trade has a net, and for no trades', () => {
    expect(view.netEquitySeries([t(1, '2026-11-16 09:30:00', null)])).toEqual({
      points: [],
      used: 0,
      skipped: 1,
    })
    expect(view.netEquitySeries([])).toEqual({ points: [], used: 0, skipped: 0 })
  })

  it('does not rely on gross P&L: the series ignores the gross field', () => {
    const out = view.netEquitySeries([
      { ...t(1, '2026-11-16 09:30:00', 87.5), realized_pnl: 100 } as ReturnType<typeof t>,
    ])
    expect(out.points[0].value).toBe(87.5)
  })
})

describe('chart and table helpers', () => {
  it('writes points with a sign and a dash for unknown', () => {
    expect(view.fmtPoints(12.5)).toBe('+12.50')
    expect(view.fmtPoints(-3)).toBe('-3.00')
    expect(view.fmtPoints(0)).toBe('0.00')
    expect(view.fmtPoints(null)).toBe('—')
    expect(view.fmtDistance(null)).toBe('—')
    expect(view.fmtDistance(0)).toBe('0.00')
  })

  it('puts the sign in the label whatever the currency formatter does', () => {
    const money = (v: number) => `Rs ${v.toFixed(2)}`
    expect(view.signedMoney(5, money)).toBe('+Rs 5.00')
    expect(view.signedMoney(-5, money)).toBe('-Rs 5.00')
    expect(view.signedMoney(0, money)).toBe('Rs 0.00')
  })

  it('scales bars to the largest absolute value, positive and negative alike', () => {
    const bars = view.calendarBars([
      {
        key: '2026-10',
        positions: 4,
        wins: 3,
        losses: 1,
        win_rate: 75,
        realized_gross: 500,
        charges: 40,
        net_of_charges: 460,
        charges_unavailable_positions: 0,
        net_unavailable_positions: 1,
      },
      {
        key: '2026-11',
        positions: 2,
        wins: 0,
        losses: 2,
        win_rate: 0,
        realized_gross: -200,
        charges: 20,
        net_of_charges: -230,
        charges_unavailable_positions: 0,
        net_unavailable_positions: 0,
      },
    ])
    expect(bars.map((b) => [b.label, b.value, b.count])).toEqual([
      ['2026-10', 460, 3],
      ['2026-11', -230, 2],
    ])
    expect(bars[0].pct).toBe(100)
    expect(bars[1].pct).toBe(50)
    expect(view.calendarBars(null)).toEqual([])
  })

  it('orders the symbol bars best first', () => {
    const rows = [
      { ...summary().by_symbol[0], key: 'A', net_of_charges: -10 },
      { ...summary().by_symbol[0], key: 'B', net_of_charges: 30 },
    ]
    expect(view.symbolBars(rows).map((b) => b.label)).toEqual(['B', 'A'])
  })

  it('plots only available positions that have both MAE and MFE', () => {
    const base = {
      id: 1,
      symbol: 'X',
      exchange: 'NSE',
      direction: 'LONG' as const,
      category: 'intraday' as const,
      opened_at: '',
      closed_at: '',
      closed_quantity: 1,
      points_per_unit: 1,
      reason: null,
      reason_text: null,
      mae_amount: 1,
      mfe_amount: 1,
      minute_bar_precision: true,
      profit_capture: null,
    }
    const pts = view.scatterPoints([
      { ...base, id: 1, net_pnl: 10, status: 'available', mae_points: 1, mfe_points: 2 },
      { ...base, id: 2, net_pnl: -10, status: 'available', mae_points: 3, mfe_points: 1 },
      { ...base, id: 3, net_pnl: 0, status: 'available', mae_points: 0, mfe_points: 0 },
      { ...base, id: 4, net_pnl: 5, status: 'unavailable', mae_points: null, mfe_points: null },
      { ...base, id: 5, net_pnl: 5, status: 'available', mae_points: null, mfe_points: 2 },
    ])
    expect(pts.map((p) => [p.id, p.kind])).toEqual([
      [1, 'winner'],
      [2, 'loser'],
      [3, 'even'],
    ])
    // a real zero MAE is plotted at 0; an unavailable one is never plotted
    expect(pts[2]).toMatchObject({ mae: 0, mfe: 0 })
  })

  it('joins excursions to the rows only for the same build', () => {
    const ex = { build_id: 'b1', positions: [{ id: 7 }] } as never
    expect(view.excursionIndex(ex, 'b1').has(7)).toBe(true)
    expect(view.excursionIndex(ex, 'b2').size).toBe(0)
    expect(view.excursionIndex(undefined, 'b1').size).toBe(0)
  })

  it('writes the unresolved warning only above 0', () => {
    const u = { unresolved_fills: 1, orphan_fills: 0, total: 1, start: 'a', end: 'b' }
    expect(view.unresolvedWarningText(u)).toBe(
      '1 fill in the selected range could not be attributed and is excluded from completed-trade statistics.'
    )
    expect(view.unresolvedWarningText({ ...u, total: 4 })).toBe(
      '4 fills in the selected range could not be attributed and are excluded from completed-trade statistics.'
    )
    expect(view.unresolvedWarningText({ ...u, total: 0 })).toBeNull()
    expect(view.unresolvedWarningText(null)).toBeNull()
    expect(view.unresolvedWarningText(undefined)).toBeNull()
  })

  it('explains unclassified reasons in plain words, never the raw code', () => {
    expect(view.categoryReasonText('opening_unknown')).toBe(
      'the fill that opened it is not in the ledger'
    )
    expect(view.categoryReasonText(null)).toBe('not classified')
    expect(view.unclassifiedReasonsText({ opening_unknown: 2, orphan_fills: 1 })).toBe(
      '2: the fill that opened it is not in the ledger; 1: some of its fills could not be placed'
    )
  })

  it('names the current streak', () => {
    expect(view.streakText({ type: 'win', length: 1 })).toBe('1 win')
    expect(view.streakText({ type: 'win', length: 3 })).toBe('3 wins')
    expect(view.streakText({ type: 'loss', length: 2 })).toBe('2 losses')
    expect(view.streakText({ type: null, length: 0 })).toBe('none')
  })
})
