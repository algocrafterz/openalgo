import { describe, expect, it } from 'vitest'
import type { IdeaCard } from '@/api/backtestResults'
import { makeCard, makeLatest } from '@/test/backtestResultsFixtures'
import {
  EMPTY_FILTERS,
  facetOptions,
  facetOptionsFor,
  filterIdeas,
  filtersFromParams,
  filtersToParams,
  groupByStrategyCategory,
  ideaTags,
  legacyOptionsFor,
  sortIdeas,
  tagOptions,
  tagOptionsFor,
} from './backtestResults'

function cls(over: Partial<NonNullable<IdeaCard['classification']>> = {}) {
  return {
    strategy_category: 'Mean Reversion',
    strategy_tags: [],
    trading_style: 'Intraday',
    direction: 'Long',
    timeframe: '5m',
    market: 'NSE Equities',
    ...over,
  }
}

const A = makeCard({
  id: 'A',
  title: 'VWAP Mean Reversion',
  strategy_category: 'Mean Reversion',
  strategy_tags: ['VWAP', 'Pullback'],
  research_state: 'active',
  classification: cls(),
})
const B = makeCard({
  id: 'B',
  title: 'Opening Breakout',
  strategy_category: 'Breakout',
  strategy_tags: ['Pullback'],
  research_state: 'promising',
  classification: cls({
    strategy_category: 'Breakout',
    trading_style: 'Swing',
    direction: 'Short',
    timeframe: '1d',
    market: 'NSE F&O',
  }),
})
const C = makeCard({ id: 'C', title: 'Uncategorised', strategy_tags: ['Misc'] })
const ALLI = [A, B, C]
const f = (p: Partial<typeof EMPTY_FILTERS>) => ({ ...EMPTY_FILTERS, ...p })
const idsOf = (l: IdeaCard[]) => l.map((i) => i.id)

describe('facet filters', () => {
  it('filters each classification facet', () => {
    expect(idsOf(filterIdeas(ALLI, f({ tradingStyle: 'Swing' })))).toEqual(['B'])
    expect(idsOf(filterIdeas(ALLI, f({ direction: 'Long' })))).toEqual(['A'])
    expect(idsOf(filterIdeas(ALLI, f({ market: 'NSE F&O' })))).toEqual(['B'])
    expect(idsOf(filterIdeas(ALLI, f({ timeframe: '5m' })))).toEqual(['A'])
    expect(idsOf(filterIdeas(ALLI, f({ researchState: 'promising' })))).toEqual(['B'])
    expect(idsOf(filterIdeas(ALLI, f({ strategyCategory: 'Breakout' })))).toEqual(['B'])
  })

  it('counts an idea without a category as Other', () => {
    expect(idsOf(filterIdeas(ALLI, f({ strategyCategory: 'Other' })))).toEqual(['C'])
    expect(facetOptions(ALLI, 'strategyCategory')).toEqual([
      { value: 'Breakout', count: 1 },
      { value: 'Mean Reversion', count: 1 },
      { value: 'Other', count: 1 },
    ])
  })

  it('derives options with counts and skips ideas lacking the value', () => {
    expect(facetOptions(ALLI, 'tradingStyle')).toEqual([
      { value: 'Intraday', count: 1 },
      { value: 'Swing', count: 1 },
    ])
  })

  it('combines facets with AND', () => {
    expect(idsOf(filterIdeas(ALLI, f({ tradingStyle: 'Intraday', market: 'NSE F&O' })))).toEqual([])
    expect(
      idsOf(filterIdeas(ALLI, f({ tradingStyle: 'Intraday', market: 'NSE Equities' })))
    ).toEqual(['A'])
  })

  it('uses the calculated verdict over the legacy one', () => {
    const d = makeCard({
      id: 'D',
      verdict: 'no-edge',
      latest_run: makeLatest({ run_verdict: 'EDGE' }),
    })
    expect(idsOf(filterIdeas([d], f({ verdict: 'edge' })))).toEqual(['D'])
    expect(idsOf(filterIdeas([d], f({ verdict: 'no-edge' })))).toEqual([])
    expect(facetOptions([d], 'verdict')).toEqual([{ value: 'edge', count: 1 }])
  })
})

describe('tags', () => {
  it('unions strategy_tags and classification tags de-duplicated case-insensitively', () => {
    expect(ideaTags(A)).toEqual(['VWAP', 'Pullback'])
  })

  it('offers tag options with counts from strategy_tags', () => {
    expect(tagOptions(ALLI)).toEqual([
      { value: 'Misc', count: 1 },
      { value: 'Pullback', count: 2 },
      { value: 'VWAP', count: 1 },
    ])
  })

  it('requires every selected tag', () => {
    expect(idsOf(filterIdeas(ALLI, f({ tags: ['pullback'] })))).toEqual(['A', 'B'])
    expect(idsOf(filterIdeas(ALLI, f({ tags: ['Pullback', 'vwap'] })))).toEqual(['A'])
    expect(idsOf(filterIdeas(ALLI, f({ tags: ['Misc'] })))).toEqual(['C'])
  })
})

describe('search', () => {
  it('matches name, category and tags case-insensitively by substring', () => {
    expect(idsOf(filterIdeas(ALLI, f({ search: 'opening' })))).toEqual(['B'])
    expect(idsOf(filterIdeas(ALLI, f({ search: 'MEAN rev' })))).toEqual(['A'])
    expect(idsOf(filterIdeas(ALLI, f({ search: 'misc' })))).toEqual(['C'])
    expect(idsOf(filterIdeas(ALLI, f({ search: 'pullb' })))).toEqual(['A', 'B'])
  })

  it('combines with filters', () => {
    expect(idsOf(filterIdeas(ALLI, f({ search: 'pullb', direction: 'Short' })))).toEqual(['B'])
  })
})

describe('url params', () => {
  it('round trips filters and grouping', () => {
    const filters = f({
      search: 'vwap',
      strategyCategory: 'Mean Reversion',
      tradingStyle: 'Intraday',
      direction: 'Long',
      market: 'NSE Equities',
      timeframe: '5m',
      researchState: 'active',
      verdict: 'edge',
      status: 'live',
      horizon: 'swing',
      tags: ['VWAP', 'Pullback'],
    })
    const back = filtersFromParams(new URLSearchParams(filtersToParams(filters, true).toString()))
    expect(back.filters).toEqual(filters)
    expect(back.groupByCategory).toBe(true)
  })

  it('writes nothing for defaults', () => {
    expect(filtersToParams(EMPTY_FILTERS, false).toString()).toBe('')
    expect(filtersFromParams(new URLSearchParams()).filters).toEqual(EMPTY_FILTERS)
  })
})

describe('groupByStrategyCategory', () => {
  it('groups alphabetically with Other last and keeps incoming order', () => {
    const sorted = sortIdeas(ALLI, 'id', 'desc')
    const groups = groupByStrategyCategory([
      ...sorted,
      makeCard({ id: 'A2', strategy_category: 'Mean Reversion' }),
    ])
    expect(groups.map((g) => g.category)).toEqual(['Breakout', 'Mean Reversion', 'Other'])
    expect(idsOf(groups[1].ideas)).toEqual(['A', 'A2'])
  })
})

describe('unknown url values', () => {
  it('drops facet, status, horizon and tag values that no loaded idea carries', () => {
    const params = new URLSearchParams(
      'cat=Bogus&style=Nope&verdict=zzz&status=ghost&tag=Nothing&tag=VWAP&dir=Long'
    )
    const { filters } = filtersFromParams(params, ALLI)
    expect(filters.strategyCategory).toBe('all')
    expect(filters.tradingStyle).toBe('all')
    expect(filters.verdict).toBe('all')
    expect(filters.status).toBe('all')
    expect(filters.tags).toEqual(['VWAP'])
    expect(filters.direction).toBe('Long')
    expect(idsOf(filterIdeas(ALLI, filters))).toEqual(['A'])
  })

  it('keeps every value when no ideas are known yet', () => {
    const { filters } = filtersFromParams(new URLSearchParams('cat=Bogus'), [])
    expect(filters.strategyCategory).toBe('Bogus')
  })
})

describe('tags in the url', () => {
  it('round trips a tag containing a comma', () => {
    const filters = f({ tags: ['a,b', 'c'] })
    const back = filtersFromParams(new URLSearchParams(filtersToParams(filters, false).toString()))
    expect(back.filters.tags).toEqual(['a,b', 'c'])
  })

  it('ignores the removed tags, category and library params', () => {
    const { filters } = filtersFromParams(new URLSearchParams('tags=x,y&category=a&library=b'))
    expect(filters).toEqual(EMPTY_FILTERS)
  })
})

describe('counts reflect the other filters', () => {
  it('facet counts equal the rows you would get by choosing the option', () => {
    const base = f({ search: 'pullb' })
    for (const facet of ['strategyCategory', 'tradingStyle', 'direction', 'market'] as const) {
      for (const o of facetOptionsFor(ALLI, base, facet)) {
        expect(o.count).toBe(filterIdeas(ALLI, { ...base, [facet]: o.value }).length)
      }
    }
  })

  it('a facet count changes when another facet is selected', () => {
    const before = facetOptionsFor(ALLI, f(), 'direction')
    const after = facetOptionsFor(ALLI, f({ tradingStyle: 'Intraday' }), 'direction')
    expect(after).not.toEqual(before)
    for (const o of after) {
      expect(o.count).toBe(
        filterIdeas(ALLI, f({ tradingStyle: 'Intraday', direction: o.value })).length
      )
    }
  })

  it('keeps options with no matching rows at zero and ignores its own selection', () => {
    const opts = facetOptionsFor(ALLI, f({ tradingStyle: 'Intraday' }), 'tradingStyle')
    expect(opts.map((o) => o.value)).toEqual(['Intraday', 'Swing'])
    expect(opts.find((o) => o.value === 'Swing')?.count).toBe(1)
  })

  it('tag counts equal the rows after adding the tag', () => {
    const base = f({ tags: ['VWAP'] })
    for (const o of tagOptionsFor(ALLI, base)) {
      expect(o.count).toBe(filterIdeas(ALLI, { ...base, tags: ['VWAP', o.value] }).length)
    }
  })

  it('status and horizon counts equal the rows after choosing the option', () => {
    const base = f({ search: 'pullb' })
    for (const field of ['status', 'horizon'] as const) {
      for (const o of legacyOptionsFor(ALLI, base, field)) {
        expect(o.count).toBe(filterIdeas(ALLI, { ...base, [field]: o.value }).length)
      }
    }
  })
})
