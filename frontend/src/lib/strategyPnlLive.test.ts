import { describe, expect, it } from 'vitest'
import type { StrategyLeg, StrategyPnl } from '@/api/strategyPnl'
import { applyLivePrices, legPriceKey, openLegItems } from './strategyPnlLive'

function leg(overrides: Partial<StrategyLeg> = {}): StrategyLeg {
  return {
    symbol: 'SBIN',
    exchange: 'NSE',
    product: 'MIS',
    quantity: 10,
    average_price: 100,
    ltp: 100,
    realized: 0,
    today_realized: 0,
    unrealized: 0,
    ...overrides,
  }
}

function strategy(legs: StrategyLeg[], overrides: Partial<StrategyPnl> = {}): StrategyPnl {
  return {
    strategy: 'ORB',
    realized: 50,
    today_realized: 50,
    unrealized: 0,
    total: 50,
    today_total: 50,
    open_quantity: legs.reduce((a, l) => a + l.quantity, 0),
    unpriced_legs: 0,
    legs,
    ...overrides,
  }
}

describe('openLegItems', () => {
  it('lists each open symbol once and skips flat legs', () => {
    const items = openLegItems([
      strategy([leg(), leg({ symbol: 'INFY' }), leg({ symbol: 'TCS', quantity: 0 })]),
      strategy([leg()], { strategy: 'OTHER' }),
    ])

    expect(items.map((i) => i.symbol).sort()).toEqual(['INFY', 'SBIN'])
  })
})

describe('applyLivePrices', () => {
  it('reprices a long leg and rolls totals up', () => {
    const prices = new Map([[legPriceKey('NSE', 'SBIN'), 110]])

    const [result] = applyLivePrices([strategy([leg()])], prices)

    expect(result.legs[0].ltp).toBe(110)
    expect(result.legs[0].unrealized).toBe(100)
    expect(result.unrealized).toBe(100)
    expect(result.total).toBe(150)
    expect(result.today_total).toBe(150)
  })

  it('reprices a short leg with the sign reversed', () => {
    const prices = new Map([[legPriceKey('NSE', 'SBIN'), 110]])

    const [result] = applyLivePrices([strategy([leg({ quantity: -10 })])], prices)

    expect(result.legs[0].unrealized).toBe(-100)
  })

  it('keeps server figures when no live price is available', () => {
    const original = strategy([leg({ unrealized: 42, ltp: 104 })], { unrealized: 42 })

    const [result] = applyLivePrices([original], new Map())

    expect(result.legs[0].unrealized).toBe(42)
    expect(result.unrealized).toBe(42)
  })

  it('prices a leg the server could not price once a live price arrives', () => {
    const original = strategy([leg({ ltp: null })], { unpriced_legs: 1 })
    const prices = new Map([[legPriceKey('NSE', 'SBIN'), 105]])

    const [result] = applyLivePrices([original], prices)

    expect(result.unpriced_legs).toBe(0)
    expect(result.unrealized).toBe(50)
  })

  it('leaves flat legs untouched', () => {
    const flat = leg({ quantity: 0, ltp: 100, unrealized: 0 })
    const prices = new Map([[legPriceKey('NSE', 'SBIN'), 130]])

    const [result] = applyLivePrices([strategy([flat])], prices)

    expect(result.legs[0].unrealized).toBe(0)
  })

  it('does not mutate its input', () => {
    const original = strategy([leg()])
    const prices = new Map([[legPriceKey('NSE', 'SBIN'), 110]])

    applyLivePrices([original], prices)

    expect(original.legs[0].unrealized).toBe(0)
    expect(original.total).toBe(50)
  })
})
