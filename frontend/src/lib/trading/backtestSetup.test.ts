import { describe, expect, it } from 'vitest'
import type { DeclaredSettings } from './backtestInputs'
import {
  advancedFacts,
  chartFacts,
  costsNote,
  declaredFacts,
  lookbackLabel,
  NOT_READ,
  plain,
  rangeNote,
} from './backtestSetup'

const DECLARED: DeclaredSettings = {
  capital: 100000,
  currency: 'INR',
  qty: 10,
  qtyType: 'units',
  product: 'MIS',
  fillOn: 'next_open',
  slippage: 2,
  commission: 0.03,
  commissionType: 'percent',
  pyramiding: 1,
  closeOnSessionEnd: true,
}

describe('chartFacts', () => {
  it("are the chart's, and say so when there is no chart", () => {
    expect(chartFacts({ symbol: 'SBIN', exchange: 'NSE', interval: '5m' })).toEqual([
      { key: 'instrument', label: 'Instrument', value: 'SBIN (NSE)', source: 'chart' },
      { key: 'timeframe', label: 'Timeframe', value: '5m', source: 'chart' },
    ])
    expect(chartFacts(null).map((f) => f.value)).toEqual(['No chart open', 'No chart open'])
  })

  it('leave out the exchange rather than print empty brackets', () => {
    expect(chartFacts({ symbol: 'X', exchange: '', interval: 'D' })[0].value).toBe('X')
  })
})

describe('declaredFacts', () => {
  it("read the script's own declaration and name the script as the source", () => {
    const facts = Object.fromEntries(declaredFacts(DECLARED, null, false).map((f) => [f.key, f]))
    expect(facts.capital.value).toBe('100,000 INR')
    expect(facts.size.value).toBe('10 units')
    expect(facts.commission.value).toBe('0.03 percent')
    expect(facts.slippage.value).toBe('2 ticks')
    expect(Object.values(facts).every((f) => f.source === 'script')).toBe(true)
  })

  it('show a size taken from an input as set by the trader, at the size the run will send', () => {
    const size = declaredFacts(DECLARED, 25, true).find((f) => f.key === 'size')
    expect(size?.value).toBe('25 units')
    expect(size?.source).toBe('you')
    expect(size?.hint).toContain('input')
  })

  it('say not read yet, never zero, when there is no declaration', () => {
    const facts = declaredFacts(null, null, false)
    expect(facts.map((f) => f.value)).toEqual([NOT_READ, NOT_READ, NOT_READ, NOT_READ])
  })
})

describe('plain', () => {
  it('breaks an identifier into the words it is made of, and leaves words alone', () => {
    expect(plain('perTrade')).toBe('per trade')
    expect(plain('next_open')).toBe('next open')
    expect(plain('percent')).toBe('percent')
    expect(plain('')).toBe('')
  })
})

describe('costsNote', () => {
  it('says nothing without a declaration', () => {
    expect(costsNote(null)).toBeNull()
  })

  it('names the statutory charges the panel does not add, whatever the script declares', () => {
    for (const note of [
      costsNote(DECLARED),
      costsNote({ ...DECLARED, commission: 0, slippage: 0 }),
    ]) {
      expect(note).toContain('adds no charges of its own')
      for (const charge of ['STT', 'exchange fees', 'GST', 'stamp duty']) {
        expect(note).toContain(charge)
      }
    }
  })

  it('warns that figures are before costs only when neither commission nor slippage is declared', () => {
    expect(costsNote(DECLARED)).not.toContain('before trading costs')
    expect(costsNote({ ...DECLARED, commission: 0 })).not.toContain('before trading costs')
    expect(costsNote({ ...DECLARED, slippage: 0 })).not.toContain('before trading costs')
    expect(costsNote({ ...DECLARED, commission: 0, slippage: 0 })).toContain('before trading costs')
  })
})

describe('the date range note', () => {
  it('labels a lookback the way a trader says it', () => {
    expect(lookbackLabel(60)).toBe('2 months')
    expect(lookbackLabel(730)).toBe('2 years')
    expect(lookbackLabel(365)).toBe('1 year')
    expect(lookbackLabel(1825)).toBe('5 years')
    expect(lookbackLabel(3650)).toBe('10 years')
    expect(lookbackLabel(45)).toBe('45 days')
  })

  it('says the default follows the timeframe until the trader sets dates', () => {
    expect(rangeNote('5m', false)).toBe(
      'Default for 5m: the last 2 months. It follows the timeframe until you set dates.'
    )
    expect(rangeNote('D', false)).toContain('the last 2 years')
    expect(rangeNote('5m', true)).toContain('Dates set by you')
    expect(rangeNote('', false)).toBe('')
  })
})

describe('advancedFacts', () => {
  it('lists how the script trades, with plain yes and no', () => {
    expect(advancedFacts(DECLARED)).toEqual([
      { label: 'Fills on', value: 'next open' },
      { label: 'Pyramiding', value: '1' },
      { label: 'Product', value: 'MIS' },
      { label: 'Closes at session end', value: 'Yes' },
    ])
    expect(
      advancedFacts({ ...DECLARED, closeOnSessionEnd: false, product: '', fillOn: '' })
    ).toEqual([
      { label: 'Fills on', value: '-' },
      { label: 'Pyramiding', value: '1' },
      { label: 'Product', value: '-' },
      { label: 'Closes at session end', value: 'No' },
    ])
  })
})
