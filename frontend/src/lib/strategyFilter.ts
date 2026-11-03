/**
 * Strategy filter for the Performance page: which strategies are in the report.
 *
 * It lives in the URL as repeated params (`?strategy=A&strategy=B&exclude=C`),
 * not a comma list, so a name that holds a comma cannot break it. A single
 * `?strategy=A` from an older link is a one-item include list.
 *
 * Nothing selected is its own state, kept in the page URL as `select=none`.
 * It is never sent to the server.
 *
 * Resolution: start from the include list (all strategies when it is empty),
 * then remove the excluded ones. A name in both lists is excluded.
 */

export interface StrategyFilter {
  include: string[]
  exclude: string[]
  /** True when the user cleared every strategy: the report is empty by choice. */
  none?: boolean
}

export type StrategyState = 'include' | 'exclude' | 'neutral'

export const WHOLE_PORTFOLIO: StrategyFilter = { include: [], exclude: [] }

function cleanNames(values: string[]): string[] {
  const seen = new Set<string>()
  for (const raw of values) {
    const name = raw.trim()
    if (name) seen.add(name)
  }
  return [...seen]
}

/** Reads the filter from URL params; blank names are dropped and repeats kept once. */
export function parseStrategyFilter(params: URLSearchParams): StrategyFilter {
  if (params.get('select') === 'none') return { include: [], exclude: [], none: true }
  return {
    include: cleanNames(params.getAll('strategy')),
    exclude: cleanNames(params.getAll('exclude')),
  }
}

/** Writes the filter onto a copy of `params`, replacing any earlier filter params. */
export function writeStrategyFilter(
  params: URLSearchParams,
  filter: StrategyFilter
): URLSearchParams {
  const next = new URLSearchParams(params)
  next.delete('strategy')
  next.delete('exclude')
  next.delete('select')
  if (filter.none) {
    next.set('select', 'none')
    return next
  }
  for (const name of cleanNames(filter.include)) next.append('strategy', name)
  for (const name of cleanNames(filter.exclude)) next.append('exclude', name)
  return next
}

/** Only the filter params, for an API request. */
export function strategyFilterParams(filter: StrategyFilter): URLSearchParams {
  const out = writeStrategyFilter(new URLSearchParams(), filter)
  out.delete('select')
  return out
}

/** Strategies that make up the report, out of every strategy known. */
export function resolveStrategies(filter: StrategyFilter, all: string[]): string[] {
  const include = new Set(filter.include)
  const exclude = new Set(filter.exclude)
  const start = include.size > 0 ? all.filter((name) => include.has(name)) : all
  return start.filter((name) => !exclude.has(name))
}

export function strategyState(filter: StrategyFilter, name: string): StrategyState {
  if (filter.none) return 'neutral'
  if (filter.exclude.includes(name)) return 'exclude'
  if (filter.include.includes(name)) return 'include'
  return 'neutral'
}

/** Moves one strategy to the next state: neutral, include, exclude, back to neutral. */
export function cycleStrategy(filter: StrategyFilter, name: string): StrategyFilter {
  if (filter.none) return { include: [name], exclude: [] }
  const without = {
    include: filter.include.filter((n) => n !== name),
    exclude: filter.exclude.filter((n) => n !== name),
  }
  switch (strategyState(filter, name)) {
    case 'neutral':
      return { include: [...without.include, name], exclude: without.exclude }
    case 'include':
      return { include: without.include, exclude: [...without.exclude, name] }
    default:
      return without
  }
}

/** Only this strategy. */
export function onlyStrategy(name: string): StrategyFilter {
  return { include: [name], exclude: [] }
}

/** Every known strategy named explicitly as included. */
export function selectAll(all: string[]): StrategyFilter {
  return { include: cleanNames(all), exclude: [] }
}

/** Nothing selected. */
export function clearAll(): StrategyFilter {
  return { include: [], exclude: [], none: true }
}

export function isWholePortfolio(filter: StrategyFilter): boolean {
  return !filter.none && filter.include.length === 0 && filter.exclude.length === 0
}
