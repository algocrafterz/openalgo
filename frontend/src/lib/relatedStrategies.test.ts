import { describe, expect, it } from 'vitest'
import { makeCard } from '@/test/backtestResultsFixtures'
import { relatedStrategies } from './relatedStrategies'

const cur = makeCard({
  id: 'CUR',
  title: 'Current',
  strategy_category: 'Mean Reversion',
  strategy_tags: ['VWAP', 'Pullback'],
})

describe('relatedStrategies', () => {
  it('lists same-category siblings, excluding the current idea', () => {
    const r = relatedStrategies(cur, [
      cur,
      makeCard({ id: 'S2', title: 'Zeta', strategy_category: 'Mean Reversion' }),
      makeCard({ id: 'S1', title: 'Alpha', strategy_category: 'Mean Reversion' }),
      makeCard({ id: 'X', title: 'Other cat', strategy_category: 'Breakout' }),
    ])
    expect(r.sameCategory.map((i) => i.id)).toEqual(['S1', 'S2'])
    expect(r.sharedTags).toEqual([])
  })

  it('orders shared tags by count then title, case-insensitively, with legacy tags', () => {
    const r = relatedStrategies(cur, [
      cur,
      makeCard({ id: 'one', title: 'B one', strategy_tags: ['vwap'] }),
      makeCard({ id: 'two', title: 'Z two', strategy_tags: ['VWAP', 'Pullback'] }),
      makeCard({ id: 'leg', title: 'A legacy', tags: ['pullback'] }),
      makeCard({ id: 'none', title: 'None', strategy_tags: ['x'] }),
    ])
    expect(r.sharedTags.map((s) => s.idea.id)).toEqual(['two', 'leg', 'one'])
    expect(r.sharedTags[0].tags).toEqual(['VWAP', 'Pullback'])
  })

  it('is empty when the current idea has no category or tags', () => {
    const bare = makeCard({ id: 'bare' })
    const r = relatedStrategies(bare, [bare, makeCard({ id: 'o' }), cur])
    expect(r).toEqual({ sameCategory: [], sharedTags: [] })
  })
})
