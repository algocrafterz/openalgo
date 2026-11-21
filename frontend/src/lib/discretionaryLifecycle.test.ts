import { describe, expect, it } from 'vitest'
import { buildLifecycle, fmtNet, type LifecycleFill } from './discretionaryLifecycle'

let ref = 100
function fill(
  side: 'BUY' | 'SELL',
  qty: number,
  price: number,
  time: string,
  order: string,
  fillId: string,
  role: 'ENTRY' | 'EXIT'
): LifecycleFill {
  ref += 1
  return {
    side,
    qty_used: qty,
    price,
    fill_time: `2026-11-20 ${time}`,
    order_id: order,
    fill_id: fillId,
    fill_ref: ref,
    role,
    charge_allocated: 1,
  }
}

/** TCS 20 Nov, trade 2 (25 BUY ... 45 SELL), in the order the endpoint can return it. */
function tcsTrade2(): LifecycleFill[] {
  const exit8 = [
    [5, 2407.0],
    [5, 2407.0],
    [5, 2407.0],
    [5, 2407.0],
    [5, 2407.36],
    [5, 2407.36],
    [5, 2407.36],
    [10, 2407.36],
  ].map(([q, p], i) =>
    fill('SELL', q, p, `10:29:${String(50 + i).padStart(2, '0')}`, 'ORD-EXIT', `x${i}`, 'EXIT')
  )
  return [
    fill('BUY', 25, 2380.0, '10:04:27', 'ORD-1', 'a1', 'ENTRY'),
    fill('BUY', 10, 2379.5, '10:07:27', 'ORD-2', 'a2', 'ENTRY'),
    fill('SELL', 25, 2383.5, '10:08:12', 'ORD-3', 'a3', 'EXIT'),
    // stored out of chronological order, as in the ledger
    fill('BUY', 21, 2385.0, '10:08:46', 'ORD-92152', 'a5', 'ENTRY'),
    fill('BUY', 4, 2385.0, '10:08:45', 'ORD-92152', 'a4', 'ENTRY'),
    fill('BUY', 10, 2397.0, '10:21:00', 'ORD-6', 'a6', 'ENTRY'),
    ...exit8.reverse(),
  ]
}

describe('buildLifecycle: TCS 20 Nov trade 2', () => {
  const lc = buildLifecycle(tcsTrade2())

  it('gives the running position +25, +35, +10, +35, +45, 0 with the right labels', () => {
    expect(lc.events.map((e) => e.netAfter)).toEqual([25, 35, 10, 35, 45, 0])
    expect(lc.events.map((e) => fmtNet(e.netAfter))).toEqual([
      '+25',
      '+35',
      '+10',
      '+35',
      '+45',
      '0',
    ])
    expect(lc.events.map((e) => e.label)).toEqual([
      'Entry',
      'Addition',
      'Partial exit',
      'Addition',
      'Addition',
      'Final exit',
    ])
    expect(lc.events.map((e) => e.side)).toEqual(['BUY', 'BUY', 'SELL', 'BUY', 'BUY', 'SELL'])
  })

  it('groups the partial fills under their order', () => {
    const add = lc.events[3]
    expect(add.orderId).toBe('ORD-92152')
    expect(add.fills).toHaveLength(2)
    expect(add.fills.map((f) => f.qty_used)).toEqual([4, 21])
    expect(add.quantity).toBe(25)
    expect(add.price).toBeCloseTo(2385.0, 6)
    expect(add.startTime).toBe('2026-11-20 10:08:45')
    expect(add.endTime).toBe('2026-11-20 10:08:46')

    const exit = lc.events[5]
    expect(exit.fills).toHaveLength(8)
    expect(exit.quantity).toBe(45)
    expect(exit.price).toBeCloseTo(2407.2, 6)
    expect(exit.charges).toBe(8)
  })

  it('is complete, has bought == sold and a peak of 45', () => {
    expect(lc.complete).toBe(true)
    expect(lc.bought).toBe(70)
    expect(lc.sold).toBe(70)
    expect(lc.peakPosition).toBe(45)
  })

  it('does not mutate its input', () => {
    const input = tcsTrade2()
    const copy = input.map((f) => f.fill_ref)
    buildLifecycle(input)
    expect(input.map((f) => f.fill_ref)).toEqual(copy)
  })
})

describe('buildLifecycle: long-to-short reversal', () => {
  // A SELL of 15 against a long of 10: 10 closes the long, the 5 excess opens a short.
  // The endpoint reports each trade with only its own part of that fill (qty_used).
  const buy = fill('BUY', 10, 100, '10:00:00', 'B1', 'f1', 'ENTRY')
  const reversingSell = fill('SELL', 15, 101, '10:05:00', 'S1', 'f2', 'EXIT')
  const cover = fill('BUY', 5, 99, '10:10:00', 'B2', 'f3', 'EXIT')

  it('shows each trade with its own running position', () => {
    const longTrade = buildLifecycle([buy, { ...reversingSell, qty_used: 10 }])
    expect(longTrade.events.map((e) => [e.label, e.netAfter])).toEqual([
      ['Entry', 10],
      ['Final exit', 0],
    ])
    expect(longTrade.complete).toBe(true)
    expect(longTrade.bought).toBe(longTrade.sold)

    const shortTrade = buildLifecycle([{ ...reversingSell, qty_used: 5, role: 'ENTRY' }, cover])
    expect(shortTrade.events.map((e) => [e.label, e.side, e.netAfter])).toEqual([
      ['Entry', 'SELL', -5],
      ['Final exit', 'BUY', 0],
    ])
    expect(shortTrade.events.map((e) => fmtNet(e.netAfter))).toEqual(['-5', '0'])
    expect(shortTrade.complete).toBe(true)
    expect(shortTrade.peakPosition).toBe(5)
  })

  it('does not report a peak for an incomplete list', () => {
    const lc = buildLifecycle([buy])
    expect(lc.complete).toBe(false)
    expect(lc.peakPosition).toBeNull()
    // an exit larger than the position would cross zero: not whole either
    const crossing = buildLifecycle([buy, reversingSell])
    expect(crossing.complete).toBe(false)
    expect(crossing.peakPosition).toBeNull()
  })
})
