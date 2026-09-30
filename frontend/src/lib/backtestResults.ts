import type { IdeaCard, LatestRun, RunMetrics, SeriesPoint, Verdict } from '@/api/backtestResults'

export const NA = 'n/a'

/** Evidence order: a verdict that survived testing outranks one that did not. */
export const VERDICT_ORDER: Verdict[] = ['edge', 'marginal', 'inconclusive', 'no-edge', 'none']

/** A metric is unknowable when the run has no trades or the value is absent. */
export function isKnown(
  metrics: { trades?: number } | null | undefined,
  value: number | null | undefined
): value is number {
  if (!metrics || metrics.trades === undefined || metrics.trades === 0) return false
  return value !== null && value !== undefined && Number.isFinite(value)
}

export function fmtNum(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? NA
    : value.toFixed(digits)
}

export function fmtSigned(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NA
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}`
}

/** Signed value that reads n/a for a run with no trades. Never renders an unknown as 0. */
export function metricText(
  metrics: { trades?: number } | null | undefined,
  value: number | null | undefined,
  format: (v: number) => string = (v) => fmtSigned(v)
): string {
  return isKnown(metrics, value) ? format(value) : NA
}

export function fmtPercent(fraction: number | null | undefined): string {
  return fraction === null || fraction === undefined || !Number.isFinite(fraction)
    ? NA
    : `${(fraction * 100).toFixed(1)}%`
}

export function signClass(value: number | null | undefined, known = true): string {
  if (!known || value === null || value === undefined) return 'text-muted-foreground'
  return value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : ''
}

/** Observed t-statistic beside the hurdle for the number of variants tried. */
export function tVsHurdle(metrics: RunMetrics): string {
  if (!isKnown(metrics, metrics.t_stat)) return NA
  const hurdle = metrics.hurdle_t
  return hurdle === undefined
    ? fmtNum(metrics.t_stat)
    : `${fmtNum(metrics.t_stat)} vs ${fmtNum(hurdle)}`
}

/** Fewer than a year of trading days means annualised-style ratios are extrapolated. */
export function isExtrapolated(metrics: RunMetrics): boolean {
  return metrics.days !== undefined && metrics.days < 252
}

/** Below this many years a result may reflect a single market regime. */
export const SHORT_HISTORY_YEARS = 3

/** Older than this and the run does not see the recent market. */
export const STALE_DATA_DAYS = 7

/** Slot count a small account can plausibly run; needing more than this is a warning. */
export const SLOT_LIMIT = 10

export interface Coverage {
  years?: number | null
  data_age_days?: number | null
}

export function fmtSpan(years: number | null | undefined): string {
  return years === null || years === undefined || !Number.isFinite(years)
    ? NA
    : `${years.toFixed(1)} years`
}

export function fmtAge(days: number | null | undefined): string {
  if (days === null || days === undefined || !Number.isFinite(days)) return NA
  if (days === 0) return 'same day'
  return days === 1 ? '1 day old' : `${days} days old`
}

/** Plain-language cautions about how much market the run saw and how stale it is. */
export function coverageNotes(coverage: Coverage): string[] {
  const notes: string[] = []
  const { years, data_age_days: age } = coverage
  if (typeof years === 'number' && years < SHORT_HISTORY_YEARS) {
    notes.push(
      `Only ${years.toFixed(1)} years of history: the result may reflect a single market regime.`
    )
  }
  if (typeof age === 'number' && age > STALE_DATA_DAYS) {
    notes.push(`The data snapshot is ${age} days old: newer market days are not reflected.`)
  }
  return notes
}

/** True when the strategy needed more simultaneous positions than a small account has. */
export function exceedsSlotLimit(metrics: RunMetrics): boolean {
  const peak = metrics.peak_concurrent
  return peak !== undefined && peak > SLOT_LIMIT
}

/**
 * The per-trade average and the t-statistic can point opposite ways: the average counts
 * every trade, the t-statistic counts each trading day once, so a few heavy signal days can
 * carry a result. Never compared for a run that made no trades.
 */
export function signsDisagree(metrics: RunMetrics): boolean {
  if (!isKnown(metrics, metrics.net_bps) || !isKnown(metrics, metrics.t_stat)) return false
  if (metrics.net_bps === 0 || metrics.t_stat === 0) return false
  return Math.sign(metrics.net_bps) !== Math.sign(metrics.t_stat)
}

export type SortKey =
  | 'evidence'
  | 'id'
  | 'status'
  | 'verdict'
  | 'date'
  | 'runs'
  | 'trades'
  | 'gross'
  | 'net'
  | 'oos'
  | 'trials'
export type SortDir = 'asc' | 'desc'

function metricOrNull(run: LatestRun | null, value: number | null | undefined): number | null {
  return run && isKnown(run.metrics, value) ? value : null
}

function sortValue(idea: IdeaCard, key: SortKey): number | string | null {
  const run = idea.latest_run
  switch (key) {
    case 'id':
      return idea.id
    case 'status':
      return idea.status
    case 'verdict':
      return VERDICT_ORDER.indexOf(idea.verdict)
    case 'date':
      return run ? run.date : null
    case 'runs':
      return idea.run_count
    case 'trades':
      return run ? run.metrics.trades : null
    case 'gross':
      return metricOrNull(run, run?.metrics.gross_bps)
    case 'net':
      return metricOrNull(run, run?.metrics.net_bps)
    case 'oos':
      return metricOrNull(run, run?.oos_net_bps)
    case 'trials':
      return run ? run.n_trials : null
    default:
      return null
  }
}

function compareEvidence(a: IdeaCard, b: IdeaCard): number {
  const hasRunA = a.latest_run ? 0 : 1
  const hasRunB = b.latest_run ? 0 : 1
  if (hasRunA !== hasRunB) return hasRunA - hasRunB
  const verdictDiff = VERDICT_ORDER.indexOf(a.verdict) - VERDICT_ORDER.indexOf(b.verdict)
  if (verdictDiff !== 0) return verdictDiff
  const dateA = a.latest_run?.date ?? ''
  const dateB = b.latest_run?.date ?? ''
  if (dateA !== dateB) return dateA < dateB ? 1 : -1
  return a.id.localeCompare(b.id)
}

/** Returns a new sorted array. Unknown values always sort last, whatever the direction. */
export function sortIdeas(ideas: IdeaCard[], key: SortKey, dir: SortDir): IdeaCard[] {
  const sign = dir === 'asc' ? 1 : -1
  return [...ideas].sort((a, b) => {
    if (key === 'evidence') return sign * compareEvidence(a, b)
    const va = sortValue(a, key)
    const vb = sortValue(b, key)
    if (va === null && vb === null) return a.id.localeCompare(b.id)
    if (va === null) return 1
    if (vb === null) return -1
    if (va === vb) return a.id.localeCompare(b.id)
    return sign * (va < vb ? -1 : 1)
  })
}

export interface IdeaFilters {
  search: string
  status: string
  verdict: string
  category: string
  horizon: string
}

export const ALL = 'all'

export const EMPTY_FILTERS: IdeaFilters = {
  search: '',
  status: ALL,
  verdict: ALL,
  category: ALL,
  horizon: ALL,
}

export function filterIdeas(ideas: IdeaCard[], f: IdeaFilters): IdeaCard[] {
  const q = f.search.trim().toLowerCase()
  return ideas.filter((i) => {
    if (f.status !== ALL && i.status !== f.status) return false
    if (f.verdict !== ALL && i.verdict !== f.verdict) return false
    if (f.category !== ALL && i.category !== f.category) return false
    if (f.horizon !== ALL && i.horizon !== f.horizon) return false
    if (!q) return true
    const haystack = [i.id, i.title, i.summary, ...(i.tags ?? [])].join(' ').toLowerCase()
    return haystack.includes(q)
  })
}

export function distinct(ideas: IdeaCard[], field: 'status' | 'verdict' | 'category' | 'horizon') {
  return [...new Set(ideas.map((i) => i[field]))].sort()
}

/** Chart data must be strictly ascending in time: sort and keep the last point per day. */
export function toLineData(points: SeriesPoint[] | undefined): { time: number; value: number }[] {
  const byDay = new Map<string, number>()
  for (const [day, value] of points ?? []) byDay.set(day, value)
  return [...byDay.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([day, value]) => ({
      time: Math.floor(new Date(`${day}T00:00:00Z`).getTime() / 1000),
      value,
    }))
}

export type DeltaVerdict = 'improved' | 'worse' | 'unchanged' | 'neutral' | 'n/a'

const LOWER_IS_BETTER = new Set(['max_dd_r'])
/** Counts and hurdles describe the test, not the result: a change is not good or bad. */
const NEUTRAL_KEYS = new Set(['trades', 'days', 'hurdle_t'])

export function judgeDelta(key: string, delta: number | null): DeltaVerdict {
  if (delta === null) return 'n/a'
  if (delta === 0) return 'unchanged'
  if (NEUTRAL_KEYS.has(key)) return 'neutral'
  const better = LOWER_IS_BETTER.has(key) ? delta < 0 : delta > 0
  return better ? 'improved' : 'worse'
}

export const METRIC_LABELS: Record<string, string> = {
  net_bps: 'Net bps per trade',
  gross_bps: 'Gross bps per trade',
  oos_net_bps: 'Out-of-sample net bps',
  trades: 'Trades',
  win_rate: 'Win rate',
  profit_factor: 'Profit factor',
  expectancy_r: 'Expectancy (R)',
  payoff: 'Payoff ratio',
  sharpe: 'Sharpe (daily, annualised)',
  t_stat: 't-statistic',
  hurdle_t: 'Hurdle t (for trials)',
  max_dd_r: 'Max drawdown (R)',
  days: 'Trading days',
  breakeven_cost_bps: 'Break-even cost (bps)',
}
