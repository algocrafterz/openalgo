import { describe, expect, it } from 'vitest'
import {
  clearAll,
  cycleStrategy,
  isWholePortfolio,
  onlyStrategy,
  parseStrategyFilter,
  resolveStrategies,
  selectAll,
  strategyFilterParams,
  strategyState,
  WHOLE_PORTFOLIO,
  writeStrategyFilter,
} from './strategyFilter'

const ALL = ['BREAKINGTRADE', 'EVWAP-EARLY-PY', 'ORB']

describe('strategyFilter', () => {
  it('reads repeated params', () => {
    const params = new URLSearchParams('strategy=BREAKINGTRADE&strategy=ORB&exclude=EVWAP-EARLY-PY')
    expect(parseStrategyFilter(params)).toEqual({
      include: ['BREAKINGTRADE', 'ORB'],
      exclude: ['EVWAP-EARLY-PY'],
    })
  })

  it('treats a legacy single strategy as a one-item include list', () => {
    expect(parseStrategyFilter(new URLSearchParams('strategy=ORB'))).toEqual({
      include: ['ORB'],
      exclude: [],
    })
  })

  it('trims, drops blanks and de-duplicates', () => {
    const params = new URLSearchParams()
    params.append('strategy', ' ORB ')
    params.append('strategy', '')
    params.append('strategy', 'ORB')
    expect(parseStrategyFilter(params).include).toEqual(['ORB'])
  })

  it('round-trips names with commas and spaces', () => {
    const filter = { include: ['A,B', 'C D'], exclude: ['E&F'] }
    const text = strategyFilterParams(filter).toString()
    expect(parseStrategyFilter(new URLSearchParams(text))).toEqual(filter)
  })

  it('replaces earlier filter params and keeps the others', () => {
    const base = new URLSearchParams('period=30d&strategy=OLD&exclude=GONE')
    const next = writeStrategyFilter(base, { include: ['ORB'], exclude: [] })
    expect(next.get('period')).toBe('30d')
    expect(next.getAll('strategy')).toEqual(['ORB'])
    expect(next.getAll('exclude')).toEqual([])
  })

  it('writes nothing for the whole portfolio', () => {
    expect(strategyFilterParams(WHOLE_PORTFOLIO).toString()).toBe('')
    expect(isWholePortfolio(WHOLE_PORTFOLIO)).toBe(true)
  })

  it('resolves: empty include means all, then excludes are removed', () => {
    expect(resolveStrategies(WHOLE_PORTFOLIO, ALL)).toEqual(ALL)
    expect(resolveStrategies({ include: [], exclude: ['ORB'] }, ALL)).toEqual([
      'BREAKINGTRADE',
      'EVWAP-EARLY-PY',
    ])
    expect(resolveStrategies({ include: ['ORB', 'BREAKINGTRADE'], exclude: [] }, ALL)).toEqual([
      'BREAKINGTRADE',
      'ORB',
    ])
  })

  it('lets an exclusion win over an inclusion', () => {
    expect(resolveStrategies({ include: ['ORB'], exclude: ['ORB'] }, ALL)).toEqual([])
  })

  it('cycles neutral, include, exclude, neutral', () => {
    let filter = cycleStrategy(WHOLE_PORTFOLIO, 'ORB')
    expect(strategyState(filter, 'ORB')).toBe('include')
    filter = cycleStrategy(filter, 'ORB')
    expect(strategyState(filter, 'ORB')).toBe('exclude')
    filter = cycleStrategy(filter, 'ORB')
    expect(strategyState(filter, 'ORB')).toBe('neutral')
    expect(filter).toEqual(WHOLE_PORTFOLIO)
  })

  it('selects all, clears all and picks one', () => {
    expect(resolveStrategies(selectAll(ALL), ALL)).toEqual(ALL)
    expect(onlyStrategy('ORB')).toEqual({ include: ['ORB'], exclude: [] })
  })

  it('round-trips the nothing-selected marker', () => {
    const text = writeStrategyFilter(new URLSearchParams('period=30d'), clearAll()).toString()
    const params = new URLSearchParams(text)
    expect(params.get('select')).toBe('none')
    expect(params.get('period')).toBe('30d')
    expect(parseStrategyFilter(params)).toEqual({ include: [], exclude: [], none: true })
  })

  it('drops strategy and exclude params when writing none', () => {
    const base = new URLSearchParams('strategy=A&exclude=B')
    const next = writeStrategyFilter(base, { include: ['X'], exclude: ['Y'], none: true })
    expect(next.getAll('strategy')).toEqual([])
    expect(next.getAll('exclude')).toEqual([])
    expect(next.get('select')).toBe('none')
  })

  it('removes the marker when writing a filter that is not none', () => {
    const base = new URLSearchParams('select=none')
    const next = writeStrategyFilter(base, { include: ['ORB'], exclude: [] })
    expect(next.has('select')).toBe(false)
    expect(next.getAll('strategy')).toEqual(['ORB'])
    expect(writeStrategyFilter(base, WHOLE_PORTFOLIO).has('select')).toBe(false)
  })

  it('never sends the marker to the API', () => {
    expect(strategyFilterParams(clearAll()).has('select')).toBe(false)
    expect(strategyFilterParams(clearAll()).toString()).toBe('')
  })

  it('cycles from none to include of that name', () => {
    const filter = cycleStrategy(clearAll(), 'ORB')
    expect(filter).toEqual({ include: ['ORB'], exclude: [] })
    expect(strategyState(clearAll(), 'ORB')).toBe('neutral')
  })

  it('clear all returns the none state, which is not the whole portfolio', () => {
    expect(clearAll()).toEqual({ include: [], exclude: [], none: true })
    expect(isWholePortfolio(clearAll())).toBe(false)
  })
})
