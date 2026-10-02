import type {
  EvaluationBlock,
  GateRow,
  IdeaCard,
  LatestRun,
  Lineage,
  RunMetrics,
  RunRecord,
  RunVerdictValue,
  SeriesPoint,
  Verdict,
} from '@/api/backtestResults'

export const NA = 'n/a'

/** Evidence order: a verdict that survived testing outranks one that did not. */
export const VERDICT_ORDER: Verdict[] = ['edge', 'marginal', 'inconclusive', 'no-edge', 'none']

const CALCULATED_TO_VERDICT: Record<RunVerdictValue, Verdict> = {
  EDGE: 'edge',
  MARGINAL: 'marginal',
  NO_EDGE: 'no-edge',
  INCONCLUSIVE: 'inconclusive',
}

/**
 * The verdict shown for an idea: the latest run's calculated verdict when the server sends one,
 * otherwise the idea's derived legacy verdict. Both describe the latest run only.
 */
export function ideaVerdict(idea: Pick<IdeaCard, 'verdict' | 'latest_run'>): Verdict {
  const calculated = idea.latest_run?.run_verdict
  return (calculated && CALCULATED_TO_VERDICT[calculated]) || idea.verdict
}

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

/**
 * The t-statistic a net figure must reach before it counts as evidence: the acceptance
 * floor, raised to the run's own trial hurdle when that is higher. Mirrors
 * `build_registry.edge_bar`.
 */
export const EDGE_T_FLOOR = 2.5

/** Below this many trades a result is too thin to lean on, whatever its t-statistic says. */
export const THIN_SAMPLE_TRADES = 200

export function edgeBar(metrics: { hurdle_t?: number } | null | undefined): number {
  return Math.max(EDGE_T_FLOOR, metrics?.hurdle_t ?? 0)
}

/**
 * Colour for a net figure. Green is reserved for a value that is positive AND
 * statistically distinguishable from zero; a positive value below the bar is amber, so a
 * lucky-looking number does not read as success. Negative is red.
 */
export function evidenceClass(
  net: number | null | undefined,
  t: number | null | undefined,
  bar: number,
  known = true
): string {
  if (!known || net === null || net === undefined) return 'text-muted-foreground'
  if (net < 0) return 'text-red-600'
  if (net === 0) return ''
  return t !== null && t !== undefined && t >= bar ? 'text-green-600' : 'text-amber-600'
}

/** `evidenceClass` for a run's headline net bps against its own t-statistic and hurdle. */
export function netClass(metrics: RunMetrics): string {
  return evidenceClass(
    metrics.net_bps,
    metrics.t_stat,
    edgeBar(metrics),
    isKnown(metrics, metrics.net_bps)
  )
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
      return VERDICT_ORDER.indexOf(ideaVerdict(idea))
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
  const verdictDiff = VERDICT_ORDER.indexOf(ideaVerdict(a)) - VERDICT_ORDER.indexOf(ideaVerdict(b))
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
  /** Legacy library group (the registry's `category`), shown as "Library group". */
  category: string
  horizon: string
  /** Strategy classification facets. An idea with no strategy category counts as Other. */
  strategyCategory: string
  tradingStyle: string
  direction: string
  market: string
  timeframe: string
  researchState: string
  /** Selected tags; an idea must carry every one of them. */
  tags: string[]
}

export const ALL = 'all'
export const OTHER_CATEGORY = 'Other'

export const EMPTY_FILTERS: IdeaFilters = {
  search: '',
  status: ALL,
  verdict: ALL,
  category: ALL,
  horizon: ALL,
  strategyCategory: ALL,
  tradingStyle: ALL,
  direction: ALL,
  market: ALL,
  timeframe: ALL,
  researchState: ALL,
  tags: [],
}

/** The idea's strategy category, or Other when none is recorded. */
export function ideaStrategyCategory(idea: IdeaCard): string {
  return (
    idea.strategy_category?.trim() ||
    idea.classification?.strategy_category?.trim() ||
    OTHER_CATEGORY
  )
}

/** Union of strategy_tags and legacy tags, de-duplicated case-insensitively (first spelling wins). */
export function ideaTags(idea: IdeaCard): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  const source = [
    ...(idea.strategy_tags ?? []),
    ...(idea.classification?.strategy_tags ?? []),
    ...(idea.tags ?? []),
  ]
  for (const raw of source) {
    const tag = typeof raw === 'string' ? raw.trim() : ''
    const key = tag.toLowerCase()
    if (!tag || seen.has(key)) continue
    seen.add(key)
    out.push(tag)
  }
  return out
}

export type FacetKey =
  | 'strategyCategory'
  | 'tradingStyle'
  | 'direction'
  | 'market'
  | 'timeframe'
  | 'researchState'
  | 'verdict'

/** The value an idea has for a facet, or null when it has none. */
export function facetValue(idea: IdeaCard, facet: FacetKey): string | null {
  switch (facet) {
    case 'strategyCategory':
      return ideaStrategyCategory(idea)
    case 'tradingStyle':
      return idea.classification?.trading_style || null
    case 'direction':
      return idea.classification?.direction || null
    case 'market':
      return idea.classification?.market || null
    case 'timeframe':
      return idea.classification?.timeframe || null
    case 'researchState':
      return idea.research_state || null
    case 'verdict':
      return ideaVerdict(idea)
  }
}

export interface FacetOption {
  value: string
  count: number
}

function sortedOptions(counts: Map<string, number>): FacetOption[] {
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => a.value.localeCompare(b.value))
}

/** Options present in the loaded ideas, each with how many ideas carry it. */
export function facetOptions(ideas: IdeaCard[], facet: FacetKey): FacetOption[] {
  const counts = new Map<string, number>()
  for (const i of ideas) {
    const v = facetValue(i, facet)
    if (v) counts.set(v, (counts.get(v) ?? 0) + 1)
  }
  return sortedOptions(counts)
}

export function tagOptions(ideas: IdeaCard[]): FacetOption[] {
  const counts = new Map<string, { label: string; count: number }>()
  for (const i of ideas) {
    for (const t of ideaTags(i)) {
      const key = t.toLowerCase()
      const cur = counts.get(key)
      counts.set(key, { label: cur?.label ?? t, count: (cur?.count ?? 0) + 1 })
    }
  }
  return [...counts.values()]
    .map(({ label, count }) => ({ value: label, count }))
    .sort((a, b) => a.value.localeCompare(b.value))
}

const FACET_FILTER_KEYS: FacetKey[] = [
  'strategyCategory',
  'tradingStyle',
  'direction',
  'market',
  'timeframe',
  'researchState',
  'verdict',
]

export function filterIdeas(ideas: IdeaCard[], f: IdeaFilters): IdeaCard[] {
  const q = f.search.trim().toLowerCase()
  const wanted = f.tags.map((t) => t.toLowerCase())
  return ideas.filter((i) => {
    if (f.status !== ALL && i.status !== f.status) return false
    if (f.category !== ALL && i.category !== f.category) return false
    if (f.horizon !== ALL && i.horizon !== f.horizon) return false
    for (const facet of FACET_FILTER_KEYS) {
      const want = f[facet]
      if (want !== ALL && facetValue(i, facet) !== want) return false
    }
    if (wanted.length > 0) {
      const have = new Set(ideaTags(i).map((t) => t.toLowerCase()))
      if (!wanted.every((t) => have.has(t))) return false
    }
    if (!q) return true
    const haystack = [
      i.id,
      i.title,
      i.summary,
      i.strategy_category ?? '',
      i.classification?.strategy_category ?? '',
      ...ideaTags(i),
    ]
      .join(' ')
      .toLowerCase()
    return haystack.includes(q)
  })
}

const PARAM_FOR: Record<string, keyof IdeaFilters> = {
  q: 'search',
  status: 'status',
  verdict: 'verdict',
  library: 'category',
  horizon: 'horizon',
  cat: 'strategyCategory',
  style: 'tradingStyle',
  dir: 'direction',
  market: 'market',
  tf: 'timeframe',
  state: 'researchState',
}

/** Only non-default filters are written, so the default URL stays clean. */
export function filtersToParams(f: IdeaFilters, groupByCategory: boolean): URLSearchParams {
  const params = new URLSearchParams()
  for (const [param, key] of Object.entries(PARAM_FOR)) {
    const v = f[key] as string
    if (v && v !== ALL) params.set(param, v)
  }
  for (const tag of f.tags) params.append('tag', tag)
  if (groupByCategory) params.set('group', 'category')
  return params
}

type LegacyField = 'status' | 'category' | 'horizon'
const LEGACY_FIELDS: LegacyField[] = ['status', 'category', 'horizon']

/** Drops facet, legacy and tag values that no loaded idea carries, so a stale link cannot hide every row. */
function dropUnknownValues(filters: IdeaFilters, ideas: IdeaCard[]): IdeaFilters {
  const next: IdeaFilters = { ...filters, tags: [...filters.tags] }
  const target = next as unknown as Record<string, string>
  for (const facet of FACET_FILTER_KEYS) {
    const want = filters[facet]
    if (want !== ALL && !facetOptions(ideas, facet).some((o) => o.value === want)) {
      target[facet] = ALL
    }
  }
  for (const field of LEGACY_FIELDS) {
    const want = filters[field]
    if (want !== ALL && !ideas.some((i) => i[field] === want)) target[field] = ALL
  }
  const known = new Set(tagOptions(ideas).map((t) => t.value.toLowerCase()))
  next.tags = next.tags.filter((t) => known.has(t.toLowerCase()))
  return next
}

/** With loaded ideas, values they do not carry are dropped; without any, everything is kept. */
export function filtersFromParams(
  params: URLSearchParams,
  ideas: IdeaCard[] = []
): {
  filters: IdeaFilters
  groupByCategory: boolean
} {
  let filters: IdeaFilters = { ...EMPTY_FILTERS, tags: [] }
  const target = filters as unknown as Record<string, string>
  for (const [param, key] of Object.entries(PARAM_FOR)) {
    const v = params.get(param)
    if (v) target[key] = v
  }
  const legacy = (params.get('tags') ?? '').split(',')
  filters.tags = [...params.getAll('tag'), ...legacy].filter(Boolean)
  if (ideas.length > 0) filters = dropUnknownValues(filters, ideas)
  return { filters, groupByCategory: params.get('group') === 'category' }
}

function mergeCounts(universe: FacetOption[], counted: FacetOption[]): FacetOption[] {
  const counts = new Map(counted.map((o) => [o.value, o.count]))
  return universe.map((o) => ({ value: o.value, count: counts.get(o.value) ?? 0 }))
}

/** Options from every idea; each count is the number of rows choosing it would show. */
export function facetOptionsFor(
  ideas: IdeaCard[],
  filters: IdeaFilters,
  facet: FacetKey
): FacetOption[] {
  const rows = filterIdeas(ideas, { ...filters, [facet]: ALL })
  return mergeCounts(facetOptions(ideas, facet), facetOptions(rows, facet))
}

export function legacyOptionsFor(
  ideas: IdeaCard[],
  filters: IdeaFilters,
  field: LegacyField
): FacetOption[] {
  const tally = (list: IdeaCard[]) => {
    const counts = new Map<string, number>()
    for (const i of list) counts.set(i[field], (counts.get(i[field]) ?? 0) + 1)
    return sortedOptions(counts)
  }
  return mergeCounts(tally(ideas), tally(filterIdeas(ideas, { ...filters, [field]: ALL })))
}

/** Each count is the number of rows after adding that tag to the current selection. */
export function tagOptionsFor(ideas: IdeaCard[], filters: IdeaFilters): FacetOption[] {
  const selected = new Set(filters.tags.map((t) => t.toLowerCase()))
  return tagOptions(ideas).map((o) => {
    const tags = selected.has(o.value.toLowerCase()) ? filters.tags : [...filters.tags, o.value]
    return { value: o.value, count: filterIdeas(ideas, { ...filters, tags }).length }
  })
}

export interface CategoryGroup {
  category: string
  ideas: IdeaCard[]
}

/** Groups keep the incoming order within each group. Alphabetical groups, Other last. */
export function groupByStrategyCategory(ideas: IdeaCard[]): CategoryGroup[] {
  const map = new Map<string, IdeaCard[]>()
  for (const i of ideas) {
    const c = ideaStrategyCategory(i)
    map.set(c, [...(map.get(c) ?? []), i])
  }
  return [...map.entries()]
    .map(([category, list]) => ({ category, ideas: list }))
    .sort((a, b) => {
      if (a.category === OTHER_CATEGORY) return 1
      if (b.category === OTHER_CATEGORY) return -1
      return a.category.localeCompare(b.category)
    })
}

export function distinct(ideas: IdeaCard[], field: 'status' | 'verdict' | 'category' | 'horizon') {
  return [...new Set(ideas.map((i) => (field === 'verdict' ? ideaVerdict(i) : i[field])))].sort()
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

export type VerdictTone = 'fail' | 'warn' | 'ok' | 'unknown'

export interface RunVerdict {
  tone: VerdictTone
  headline: string
  points: string[]
}

/** Natural order of gate ids, so G2.2 sorts before G4.5 and G10.1 after G2.2. */
function gateOrder(gate: string): number[] {
  return gate
    .replace(/^[A-Za-z]+/, '')
    .split('.')
    .map((n) => Number.parseInt(n, 10) || 0)
}

function compareGates(a: string, b: string): number {
  const [x, y] = [gateOrder(a), gateOrder(b)]
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] ?? 0) - (y[i] ?? 0)
    if (d !== 0) return d
  }
  return 0
}

/** The failed hard gate with the lowest id: edge and cost gates come before robustness ones. */
function firstHardFail(run: RunRecord): { gate: string; title: string } | null {
  const failed = (run.evaluation?.dimensions ?? [])
    .flatMap((d) => d.rows)
    .filter((row) => row.hard && row.status === 'fail')
    .sort((a, b) => compareGates(a.gate, b.gate))
  return failed.length > 0 ? { gate: failed[0].gate, title: failed[0].title } : null
}

const TREND_PHRASE: Record<Lineage['trend'], string> = {
  improving: 'improving',
  stagnant: 'flat',
  regressing: 'getting worse',
  insufficient: 'too few runs to judge',
}

/**
 * The answer a reader wants first: is there evidence of an edge, what blocks it, and is
 * more tuning likely to help. Built only from numbers already on the run, so it never
 * says more than the tables below it.
 */
export function buildVerdict(
  idea: Pick<IdeaCard, 'verdict' | 'status' | 'rejection_basis'>,
  run: RunRecord | undefined,
  lineage: Lineage | null | undefined
): RunVerdict {
  if (!run) return { tone: 'unknown', headline: 'No run recorded yet', points: [] }
  const m = run.metrics
  const bar = edgeBar(m)
  const points: string[] = []
  const t = isKnown(m, m.t_stat) ? m.t_stat : undefined
  const fail = firstHardFail(run)

  if (t !== undefined) {
    points.push(`t-statistic ${fmtNum(t)} against a bar of ${fmtNum(bar)}.`)
  }
  if (fail) points.push(`First hard gate failed: ${fail.gate} ${fail.title}.`)
  const base = run.baseline
  if (base && Number.isFinite(base.excess_net_bps)) {
    const tail = base.excess_t_stat === undefined ? '' : ` (t ${fmtSigned(base.excess_t_stat)})`
    points.push(
      base.excess_net_bps > 0 && (base.excess_t_stat ?? 0) >= bar
        ? `Beats random entry by ${fmtSigned(base.excess_net_bps, 1)} bps${tail}.`
        : `Not clearly better than random entry: ${fmtSigned(base.excess_net_bps, 1)} bps${tail}.`
    )
  }
  const oos = run.out_of_sample
  if (oos && Number.isFinite(oos.net_bps)) {
    const tail = oos.t_stat === undefined ? '' : `, t ${fmtSigned(oos.t_stat)}`
    points.push(`Out-of-sample net ${fmtSigned(oos.net_bps, 1)} bps${tail}.`)
  }
  if (m.trades > 0 && m.trades < THIN_SAMPLE_TRADES) {
    points.push(`Only ${m.trades} trades: too few to trust.`)
  }
  if (lineage && lineage.runs > 1) {
    // Judged from this run, not from the server's lineage, so an older server still works.
    const below = t !== undefined && t < bar
    points.push(
      `Across ${lineage.runs} runs the result is ${TREND_PHRASE[lineage.trend]}` +
        (below ? ', but still below the bar, so progress so far does not show an edge.' : '.')
    )
  }

  const rejected = idea.status === 'rejected' || idea.verdict === 'no-edge'
  if (rejected) {
    const basis = idea.rejection_basis ? ` (${idea.rejection_basis})` : ''
    return { tone: 'fail', headline: `No edge${basis}. More tuning is not justified.`, points }
  }
  if (fail) return { tone: 'fail', headline: `Not passing: ${fail.gate} ${fail.title}`, points }
  if (t !== undefined && t >= bar) {
    return { tone: 'ok', headline: 'Clears the evidence bar. Validate out of sample next.', points }
  }
  return { tone: 'warn', headline: 'Below the evidence bar. Not yet proven.', points }
}

/** The gate row for an id, accepting a group id such as G2 for its first row G2.1. */
export function findGateRow(
  evaluation: EvaluationBlock | null | undefined,
  gate: string
): GateRow | undefined {
  if (!evaluation) return undefined
  const rows = (evaluation.dimensions ?? []).flatMap((d) => d.rows ?? [])
  return rows.find((r) => r.gate === gate) ?? rows.find((r) => r.gate.startsWith(`${gate}.`))
}
