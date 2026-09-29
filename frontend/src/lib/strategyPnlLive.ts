import type { StrategyPnl } from '@/api/strategyPnl'
import type { PriceableItem } from '@/hooks/useLivePrice'

/** Key matching useLivePrice's `${exchange}:${symbol}` price lookups. */
export function legPriceKey(exchange: string, symbol: string): string {
  return `${exchange}:${symbol}`
}

/**
 * One priceable item per distinct open symbol across all strategies, for
 * subscribing to live prices. Flat legs need no price, and a symbol held by
 * several strategies is subscribed once.
 */
export function openLegItems(strategies: StrategyPnl[]): PriceableItem[] {
  const seen = new Map<string, PriceableItem>()
  for (const strategy of strategies) {
    for (const leg of strategy.legs) {
      if (leg.quantity === 0) continue
      const key = legPriceKey(leg.exchange, leg.symbol)
      if (seen.has(key)) continue
      seen.set(key, {
        symbol: leg.symbol,
        exchange: leg.exchange,
        ltp: leg.ltp ?? undefined,
        quantity: leg.quantity,
        average_price: leg.average_price,
      })
    }
  }
  return [...seen.values()]
}

/**
 * Re-mark open legs to live prices and roll the totals back up, using the
 * server's own formula (quantity x (ltp - average price)) so a live figure and
 * the next server refresh agree. Realized figures come from fills and are never
 * touched. Legs without a live price keep the server's numbers. Pure: returns
 * new objects.
 */
export function applyLivePrices(
  strategies: StrategyPnl[],
  prices: Map<string, number>
): StrategyPnl[] {
  return strategies.map((strategy) => {
    const legs = strategy.legs.map((leg) => {
      const live = prices.get(legPriceKey(leg.exchange, leg.symbol))
      if (leg.quantity === 0 || live === undefined || !(live > 0)) return leg
      return { ...leg, ltp: live, unrealized: leg.quantity * (live - leg.average_price) }
    })
    const unrealized = legs.reduce((sum, leg) => sum + leg.unrealized, 0)
    return {
      ...strategy,
      legs,
      unrealized,
      total: strategy.realized + unrealized,
      today_total: strategy.today_realized + unrealized,
      unpriced_legs: legs.filter((leg) => leg.quantity !== 0 && leg.ltp === null).length,
    }
  })
}
