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
  awaitingAttribution,
  awaitingAttributionText,
  breakdownVisibility,
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
  progressionText,
  reasonText,
  resolveSource,
  summarizeUnrealized,
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
    expect(fmtProfitFactor(null, 0, 0)).toBe('n/a') // all break-even: no profit and no loss
    expect(fmtProfitFactor(0, 0, 0)).toBe('n/a') // a server 0 for all break-even is not a result
    expect(fmtProfitFactor(null, 0, 0, false)).toBe('—') // no position has a net: unknown
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
    expect(fmtProfitFactor(null, 0, 0)).toBe('n/a') // no wins and no losses: not applicable
  })

  it('shows n/a for average win, average loss and payoff when a side has no trades', () => {
    const money = (v: number) => `₹${v.toFixed(2)}`
    expect(view.fmtAvgByCount(0, 0, money)).toBe('n/a') // a server 0.0 is never shown as Rs 0
    expect(view.fmtAvgByCount(null, 0, money)).toBe('n/a')
    expect(view.fmtAvgByCount(120, 3, money)).toBe('₹120.00')
    expect(view.fmtPayoffRatio(0, 50, 0, 2)).toBe('n/a')
    expect(view.fmtPayoffRatio(100, 0, 2, 0)).toBe('n/a')
    expect(view.fmtPayoffRatio(0, 0, 0, 0)).toBe('n/a')
    expect(view.fmtPayoffRatio(200, 100, 2, 2)).toBe('2.00')
  })

  it('derives break-even and the sample size for the win rate card, with no small-sample flag', () => {
    const base = { winning_trades: 6, losing_trades: 5, net_basis_trades: 12 }
    expect(view.winRateParts(base)).toEqual({ wins: 6, losses: 5, breakEven: 1, sample: 12 })
    // the sample is the plain count, whatever its size
    expect(view.winRateParts({ ...base, net_basis_trades: 30 }).sample).toBe(30)
    expect(view.winRateParts({ ...base, net_basis_trades: 11 }).breakEven).toBe(0)
    // no net-basis count sent: nothing is invented
    const old = view.winRateParts({ winning_trades: 1, losing_trades: 1 })
    expect(old).toEqual({ wins: 1, losses: 1, breakEven: null, sample: null })
  })
})

describe('behaviour text', () => {
  const noDd = { max_drawdown: 0 }
  it('says insufficient data when a key is absent, and describes timing objectively', () => {
    expect(view.winningDaysText({})).toBeNull()
    expect(view.tradesPerDayText({}).value).toBe('insufficient data')
    expect(view.reentryText({}).value).toBe('insufficient data')
    expect(view.quantityText({})).toEqual([])
    expect(view.drawdownRecoveryText({ ...noDd })).toMatchObject({ value: 'No drawdown' })
    expect(view.drawdownRecoveryText({ max_drawdown: 50 }).value).toBe('insufficient data')
    expect(view.drawdownRecoveryText(null).value).toBe('insufficient data')
    const re = view.reentryText({
      reentry_after_loss: {
        window_minutes: 15,
        losses_followed_by_entry: 4,
        reentries_within_window: 2,
        shortest_gap_minutes: 3,
      },
    })
    expect(re.value).toBe('2 of 4')
    expect(re.hint).toContain('Re-entered within 15 min after a loss')
    expect(`${re.value} ${re.hint}`).not.toMatch(/revenge|fomo|tilt|emotion/i)
  })

  it('keys quantity rows by exchange and names the exchange only when several traded the symbol', () => {
    const q = (symbol: string, exchange: string) => ({
      symbol,
      exchange,
      positions: 1,
      min_quantity: 1,
      median_quantity: 1,
      max_quantity: 1,
    })
    const rows = view.quantityText({
      quantity_by_symbol: [q('GOLD', 'MCX'), q('GOLD', 'NSE'), q('SBIN', 'NSE')],
    })
    expect(rows.map((r) => r.key)).toEqual(['MCX:GOLD', 'NSE:GOLD', 'NSE:SBIN'])
    expect(rows.map((r) => r.label)).toEqual(['GOLD (MCX)', 'GOLD (NSE)', 'SBIN'])
  })

  it('shows winning-day percent with its day count and no small-sample flag', () => {
    const t = view.winningDaysText({
      net_days: {
        total_days: 3,
        winning_days: 1,
        losing_days: 2,
        flat_days: 0,
        winning_day_pct: 33.33,
        net_unavailable_days: 0,
      },
    })
    expect(t?.value).toBe('33.3%')
    expect(t).not.toHaveProperty('small')
    expect(t?.hint).toBe('1 of 3 days net positive')
  })

  it('states recovery and non-recovery of the deepest drawdown', () => {
    const detail = {
      depth: 320,
      start_at: '2026-11-19 09:20:00',
      start_basis: 'first position opened',
      trough_at: '2026-11-19 09:50:00',
      recovered: true,
      recovered_at: '2026-11-20 10:40:00',
      duration_minutes: 1520,
    }
    expect(
      view.drawdownRecoveryText({ max_drawdown: 320, max_drawdown_detail: detail }).value
    ).toBe('Recovered after 1 d 1 h')
    const open = view.drawdownRecoveryText({
      max_drawdown: 320,
      max_drawdown_detail: { ...detail, recovered: false, recovered_at: undefined },
    })
    expect(open.value).toBe('Not recovered, 1 d 1 h')
  })

  it('writes the excursion coverage as N of M completed positions with 1-minute price bars', () => {
    expect(view.coverageText(6, 12)).toBe('6 of 12 completed positions have 1-minute price bars')
    expect(view.coverageText(1, 1)).toBe('1 of 1 completed positions have 1-minute price bars')
  })

  it('writes each excursion reason as a full phrase with its own count', () => {
    const texts = new Map([
      ['historify_incomplete', 'price data for the day is not complete yet'],
      ['missing_bars', 'price data has missing minutes'],
    ])
    expect(
      view.excursionReasonPhrases({ historify_incomplete: 6, missing_bars: 1 }, texts)
    ).toEqual([
      '6 positions: price data for the day is not complete yet',
      '1 position: price data has missing minutes',
    ])
    // a code the server sent no wording for is spelled out, never shown raw
    expect(view.excursionReasonPhrases({ some_code: 2 }, new Map())).toEqual([
      '2 positions: some code',
    ])
    expect(view.excursionReasonPhrases(undefined, texts)).toEqual([])
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

  it('words the unmatched-fills message in trader language', () => {
    expect(awaitingAttributionText(5, 5)).toBe(
      'No trades to show yet: 5 of 5 fills are not counted.'
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
    // the detail moved to the tooltip of the line
    expect(view.NET_DEFINITION_DETAIL).toContain('measured on net P&L')
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
      '1 execution(s) await attribution (30-minute window) and are not in P&L yet. Earliest: 11:20 IST.',
      '2 execution(s) today are unresolved and not in P&L.',
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
    // bars follow the NET day; the nets are set to the former gross figures so the scale is the same
    const rows = [
      { ...base, net_of_charges: 60 },
      {
        ...base,
        period: '2026-11-17',
        realized_gross: -120,
        net_of_charges: -120,
        net_charges_complete: true,
        charges_unavailable_trades: 0,
      },
      {
        ...base,
        period: '2026-11-18',
        realized_gross: 0,
        net_of_charges: 0,
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

  it('shows a row profit factor from the row alone, with no sample rule', () => {
    expect(view.rowProfitFactor({ trades: 0, profit_factor: 0, wins: 0, losses: 0 }).value).toBe(
      '—'
    )
    // a three-trade group still shows its number: the Trades column carries the sample
    const small = view.rowProfitFactor({ trades: 3, profit_factor: 2, wins: 2, losses: 1 })
    expect(small).toEqual({ value: '2.00' })
    expect(view.rowProfitFactor({ trades: 40, profit_factor: 2, wins: 25, losses: 15 }).value).toBe(
      '2.00'
    )
    expect(
      view.rowProfitFactor({ trades: 40, profit_factor: null, wins: 40, losses: 0 }).value
    ).toBe('∞')
  })

  it('names the sample behind the ratios, with no low-sample wording, and nothing for zero trades', () => {
    expect(view.sampleNote(undefined)).toBeNull()
    const one = view.sampleNote({
      completed_trades: 1,
      trading_days: 1,
      min_trades: 30,
      min_days: 10,
      sufficient: false,
    })
    expect(one?.text).toBe('Ratios based on 1 completed trade over 1 trading day.')
    expect(one?.text).not.toMatch(/low sample|indicative/i)
    const many = view.sampleNote({
      completed_trades: 12,
      trading_days: 3,
      min_trades: 30,
      min_days: 10,
      sufficient: false,
    })
    expect(many?.text).toBe('Ratios based on 12 completed trades over 3 trading days.')
    // a range with no completed trade: no sentence at all
    expect(
      view.sampleNote({
        completed_trades: 0,
        trading_days: 0,
        min_trades: 30,
        min_days: 10,
        sufficient: false,
      })
    ).toBeNull()
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

  it('draws no bar for a month or weekday with no completed position', () => {
    const row = (key: string, positions: number, net: number) => ({
      key,
      positions,
      wins: 0,
      losses: 0,
      win_rate: null,
      realized_gross: net,
      charges: 0,
      net_of_charges: net,
      charges_unavailable_positions: 0,
      net_unavailable_positions: 0,
    })
    const bars = view.calendarBars([row('Mon', 3, 300), row('Tue', 0, 0), row('Wed', 1, -100)])
    expect(bars.map((b) => b.label)).toEqual(['Mon', 'Wed'])
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

  it('writes one plain-language data warning only when a problem count is above 0', () => {
    const base = summary()
    const healthy = {
      unresolved_in_range: null,
      data_quality: dataQuality({
        awaiting_attribution: 0,
        not_placed: 0,
        prices: { requested: true, open_positions: 2, statuses: {}, missing: 0, stale: 0 },
      }),
      sync_health: { ...base.sync_health, days_without_sync: [] },
    } as never
    expect(view.materialDataWarning(healthy)).toBeNull()
    const u = { unresolved_fills: 1, orphan_fills: 0, total: 1, start: 'a', end: 'b' }
    // unmatched fills have their own quiet note now, not the warning banner
    expect(
      view.materialDataWarning({ ...(healthy as object), unresolved_in_range: u } as never)
    ).toBeNull()
    expect(
      view.unmatchedFillCount({ ...(healthy as object), unresolved_in_range: u } as never)
    ).toBe(1)
    expect(
      view.unmatchedFillCount({
        ...(healthy as object),
        unresolved_in_range: { ...u, total: 4 },
      } as never)
    ).toBe(4)
    expect(view.unmatchedFillCount(healthy as never)).toBe(0)
    // older server: the pending and unplaced counts of the period stand in
    const old = view.unmatchedFillCount({
      ...(healthy as object),
      data_quality: dataQuality({ awaiting_attribution: 2, not_placed: 1 }),
    } as never)
    expect(old).toBe(3)
    // missing prices count only when prices were requested
    const px = (requested: boolean) =>
      ({
        ...(healthy as object),
        data_quality: dataQuality({
          awaiting_attribution: 0,
          not_placed: 0,
          prices: { requested, open_positions: 2, statuses: {}, missing: 2, stale: 0 },
        }),
      }) as never
    expect(view.materialDataWarning(px(false))).toBeNull()
    expect(view.materialDataWarning(px(true))).toBe(
      '2 open positions have no current price, so open P&L may be incomplete.'
    )
    // a gap of synced days
    expect(
      view.materialDataWarning({
        ...(healthy as object),
        sync_health: { ...base.sync_health, days_without_sync: ['2026-11-12', '2026-11-13'] },
      } as never)
    ).toBe(
      'Trades from 2026-11-12, 2026-11-13 may be missing, so P&L for those days may be incomplete.'
    )
  })

  describe('data warning for the selected report', () => {
    const NOW = new Date('2026-11-18T06:00:00Z') // Wednesday 11:30 IST
    const base = summary()
    const healthy = () =>
      ({
        unresolved_in_range: null,
        data_quality: dataQuality({
          awaiting_attribution: 0,
          not_placed: 0,
          prices: { requested: true, open_positions: 0, statuses: {}, missing: 0, stale: 0 },
        }),
        sync_health: {
          ...base.sync_health,
          last_successful_sync: '2026-11-18 11:00',
          days_without_sync: [],
        },
        period: { period: '1m', start: '2026-11-01', end: '2026-11-18' },
        positions: [],
        trades: [],
      }) as never
    const with_ = (extra: object) => ({ ...(healthy() as object), ...extra }) as never
    const sync = (extra: object) =>
      with_({ sync_health: { ...base.sync_health, days_without_sync: [], ...extra } })
    const pos = (extra: object) => ({ product: 'CNC', opened_at: '2026-11-18T09:30:00', ...extra })

    it('is absent when everything is healthy', () => {
      expect(view.materialDataWarning(healthy(), NOW)).toBeNull()
    })

    it('flags open intraday positions from an earlier day that reach the selected range', () => {
      const text = view.materialDataWarning(
        with_({ positions: [pos({ product: 'MIS', opened_at: '2026-11-16T10:00:00' })] }),
        NOW
      )
      expect(text).toBe(
        '1 intraday position is still open from an earlier day, so a closing trade may be missing and your P&L may be incomplete.'
      )
      // opened today, or delivery product: normal
      expect(
        view.materialDataWarning(with_({ positions: [pos({ product: 'MIS' })] }), NOW)
      ).toBeNull()
      expect(
        view.materialDataWarning(
          with_({ positions: [pos({ product: 'CNC', opened_at: '2026-11-10T10:00:00' })] }),
          NOW
        )
      ).toBeNull()
      // opened after the selected range ended: does not touch it
      expect(
        view.materialDataWarning(
          with_({
            period: { period: 'custom', start: '2026-11-02', end: '2026-11-10' },
            positions: [pos({ product: 'MIS', opened_at: '2026-11-16T10:00:00' })],
          }),
          NOW
        )
      ).toBeNull()
    })

    it('flags trades and positions with no recorded entry only inside the selected range', () => {
      expect(view.materialDataWarning(with_({ trades: [{ opening_unknown: true }] }), NOW)).toBe(
        '1 trade or position has no recorded entry, so its P&L may be wrong.'
      )
      expect(
        view.materialDataWarning(
          with_({ positions: [pos({ opening_unknown: true }), pos({ opening_unknown: true })] }),
          NOW
        )
      ).toBe('2 trades or positions have no recorded entry, so their P&L may be wrong.')
      expect(
        view.materialDataWarning(with_({ trades: [{ opening_unknown: false }] }), NOW)
      ).toBeNull()
      expect(
        view.materialDataWarning(
          with_({
            period: { period: 'custom', start: '2026-11-02', end: '2026-11-10' },
            positions: [pos({ opening_unknown: true })],
          }),
          NOW
        )
      ).toBeNull()
    })

    it('flags a stale last collection only when the selected range reaches past it', () => {
      const stale = { last_successful_sync: '2026-11-12 15:40' }
      expect(view.materialDataWarning(sync(stale), NOW)).toBe(
        'Trades after 2026-11-12 may be missing, so your P&L may be incomplete.'
      )
      // range ended before the last collection: nothing missing in it
      expect(
        view.materialDataWarning(
          with_({
            sync_health: { ...base.sync_health, days_without_sync: [], ...stale },
            period: { period: 'custom', start: '2026-11-02', end: '2026-11-12' },
          }),
          NOW
        )
      ).toBeNull()
      // recent collection, or a weekend, or the gap days already named
      expect(
        view.materialDataWarning(sync({ last_successful_sync: '2026-11-16 15:40' }), NOW)
      ).toBeNull()
      expect(
        view.materialDataWarning(sync(stale), new Date('2026-11-21T06:00:00Z')) // Saturday
      ).toBeNull()
      const gap = view.materialDataWarning(
        sync({ ...stale, days_without_sync: ['2026-11-13'] }),
        NOW
      )
      expect(gap).toBe(
        'Trades from 2026-11-13 may be missing, so P&L for those days may be incomplete.'
      )
    })

    it('names only the missing days that fall inside the selected range', () => {
      const text = view.materialDataWarning(
        with_({
          period: { period: 'custom', start: '2026-11-05', end: '2026-11-12' },
          sync_health: {
            ...base.sync_health,
            last_successful_sync: '2026-11-18 11:00',
            days_without_sync: ['2026-11-03', '2026-11-10', '2026-11-16'],
          },
        }),
        NOW
      )
      expect(text).toBe(
        'Trades from 2026-11-10 may be missing, so P&L for those days may be incomplete.'
      )
    })

    it('keeps internal words out of every warning at once', () => {
      const text = view.materialDataWarning(
        {
          unresolved_in_range: {
            unresolved_fills: 1,
            orphan_fills: 1,
            total: 2,
            start: 'a',
            end: 'b',
          },
          data_quality: dataQuality({
            prices: { requested: true, open_positions: 1, statuses: {}, missing: 1, stale: 0 },
          }),
          sync_health: {
            ...base.sync_health,
            last_successful_sync: '2026-11-02 10:00',
            days_without_sync: [],
          },
          period: { period: '1m', start: '2026-11-01', end: '2026-11-18' },
          positions: [
            pos({ product: 'MIS', opened_at: '2026-11-16T10:00:00', opening_unknown: true }),
          ],
          trades: [{ opening_unknown: true }],
        } as never,
        NOW
      )
      expect(
        view.unmatchedFillCount({
          unresolved_in_range: {
            unresolved_fills: 1,
            orphan_fills: 1,
            total: 2,
            start: 'a',
            end: 'b',
          },
          data_quality: dataQuality(),
        } as never)
      ).toBe(2)
      expect(text).toContain('no current price')
      expect(text).toContain('Trades after 2026-11-02')
      expect(text).toContain('intraday position')
      expect(text).toContain('no recorded entry')
      expect(text).not.toMatch(
        /ledger|attribut|sync|reconcil|fills collected|Risk card|\blegs?\b|unresolved|orphan/i
      )
    })
  })

  it('warns in trader words when the newest sync day has only runs that did not complete', () => {
    const base = summary()
    const mk = (runs_by_day: Record<string, Record<string, number>>) =>
      ({
        unresolved_in_range: null,
        data_quality: dataQuality({
          awaiting_attribution: 0,
          not_placed: 0,
          prices: { requested: false, open_positions: 0, statuses: {}, missing: 0, stale: 0 },
        }),
        sync_health: { ...base.sync_health, runs_by_day, days_without_sync: [] },
      }) as never
    const failed = view.materialDataWarning(mk({ '2026-11-17': { error: 1, no_auth: 1 } }))
    expect(failed).toBe(
      'Your last sync with the broker did not complete. Recent trades may be missing. Use Refresh to try again.'
    )
    // an older failed day does not count once a later day completed
    expect(
      view.materialDataWarning(mk({ '2026-11-16': { error: 1 }, '2026-11-17': { ok: 1 } }))
    ).toBeNull()
    // a partial run completed; no runs at all or no field: nothing is invented
    expect(view.materialDataWarning(mk({ '2026-11-17': { partial: 1, error: 1 } }))).toBeNull()
    expect(view.materialDataWarning(mk({}))).toBeNull()
    expect(view.lastSyncFailed(undefined)).toBe(false)
    expect(view.lastSyncFailed({ runs_by_day: { '2026-11-17': { ok: 0 } } })).toBe(false)
  })

  it('keeps internal terms out of the data warning', () => {
    const text = view.materialDataWarning({
      unresolved_in_range: { unresolved_fills: 1, orphan_fills: 1, total: 2, start: 'a', end: 'b' },
      data_quality: dataQuality({
        prices: { requested: true, open_positions: 1, statuses: {}, missing: 1, stale: 0 },
      }),
      sync_health: {
        last_successful_sync: 'x',
        runs_by_day: {},
        days_without_sync: ['2026-11-12'],
      },
    } as never)
    expect(text).not.toMatch(/attribut|ledger|sync|reconcil|collected|unresolved|orphan/i)
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

describe('progressionText', () => {
  const money = (v: number) => `Rs ${v.toFixed(2)}`
  const pt = (index: number, net: number, equity: number, drawdown: number) =>
    ({
      index,
      id: index,
      closed_at: '2026-11-16 10:00:00',
      symbol: 'X',
      direction: 'LONG',
      net,
      equity,
      peak: 0,
      drawdown,
    }) as never
  it('reads the finish, win and loss count, high point and largest fall', () => {
    const curve = {
      points: [pt(1, -50, -50, 50), pt(2, 80, 30, 0), pt(3, -20, 10, 20)],
      used: 3,
      skipped: 0,
      maxDrawdown: 50,
      maxDrawdownAt: null,
      currentDrawdown: 20,
      peakEquity: 30,
      latestEquity: 10,
    }
    const text = progressionText(curve, 'position', money)
    expect(text).toContain('finished at Rs 10.00 after 3 trades: 1 winning, 2 losing')
    expect(text).toContain('highest point was Rs 30.00 after trade 2')
    expect(text).toContain('largest fall from a peak was Rs 50.00, reached after trade 1')
  })
  it('says so when the line never rose above zero or fell', () => {
    const curve = {
      points: [pt(1, -5, -5, 5)],
      used: 1,
      skipped: 0,
      maxDrawdown: 5,
      maxDrawdownAt: null,
      currentDrawdown: 5,
      peakEquity: 0,
      latestEquity: -5,
    }
    expect(progressionText(curve, 'position', money)).toContain('never rose above zero')
    expect(progressionText({ ...curve, points: [] }, 'position', money)).toBe('')
  })
})

describe('breakdownVisibility', () => {
  it('hides every comparison of a one-symbol, one-product, one-direction session', () => {
    const one = summary()
    const v = breakdownVisibility({
      ...one,
      by_symbol: one.by_symbol?.slice(0, 1),
      by_side: one.by_side?.slice(0, 1),
      by_product: one.by_product?.slice(0, 1),
      by_holding: undefined,
    })
    expect(v.symbol && v.side && v.product && v.points).toBe(false)
    expect(v.patterns).toEqual([])
    expect(v.noneShown).toBe(true)
  })
  it('shows long vs short whenever both directions have a trade, whatever the count', () => {
    const row = (key: string, trades: number) =>
      ({ ...summary().by_symbol?.[0], key, trades }) as never
    const base = summary()
    expect(breakdownVisibility({ ...base, by_side: [row('LONG', 8), row('SHORT', 1)] }).side).toBe(
      true
    )
    // one direction alone has nothing to compare; a direction with no trade does not count
    expect(breakdownVisibility({ ...base, by_side: [row('LONG', 8), row('SHORT', 0)] }).side).toBe(
      false
    )
    expect(breakdownVisibility({ ...base, by_side: [row('LONG', 8)] }).side).toBe(false)
  })
})
