import { describe, expect, it } from 'vitest'
import {
  actionableWarnings,
  chargesBadge,
  distinctOptions,
  filterChoices,
  fmtAge,
  fmtProfitFactor,
  fmtWinRate,
  holdingRows,
  isEmptyBook,
  netDefinitionText,
  netIncomplete,
  parseSourceParam,
  priceBadge,
  ratioCell,
  reasonText,
  resolveSource,
  summarizeUnrealized,
  syncSummary,
  totalCard,
  totalPnl,
  withheldSentence,
} from './discretionaryPnlView'
import { emptySummary, position, summary } from '@/test/discretionaryFixtures'

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
})

describe('withheld ratios', () => {
  const withheld = { sharpe_ratio: 'no capital basis' }

  it('shows -- and the reason for a withheld ratio, whatever value arrives', () => {
    expect(ratioCell('sharpe_ratio', 1.7, withheld)).toEqual({
      value: '--',
      hint: 'Not shown: no capital basis',
      withheld: true,
    })
  })

  it('shows the value when it is not withheld, and -- when it is simply missing', () => {
    expect(ratioCell('sortino_ratio', 1.234, withheld)).toMatchObject({ value: '1.23' })
    expect(ratioCell('sortino_ratio', null, withheld)).toEqual({ value: '--', withheld: false })
  })

  it('writes one sentence per reason', () => {
    expect(
      withheldSentence({
        sharpe_ratio: 'no capital basis',
        sortino_ratio: 'no capital basis',
        return_pct: 'no capital basis',
      })
    ).toBe('Sharpe, Sortino, Return %: not shown: no capital basis')
    expect(withheldSentence({})).toBe('')
    expect(withheldSentence(undefined)).toBe('')
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
