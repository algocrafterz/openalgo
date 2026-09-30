import { webClient } from './client'

export type Verdict = 'none' | 'edge' | 'marginal' | 'no-edge' | 'inconclusive'

/** Headline metrics of a run. Optional keys are omitted by the backend when unknown. */
export interface RunMetrics {
  trades: number
  gross_bps: number
  net_bps: number
  win_rate?: number
  sharpe?: number
  t_stat?: number
  hurdle_t?: number
  profit_factor?: number
  expectancy_r?: number
  payoff?: number
  max_dd_r?: number
  days?: number
  breakeven_cost_bps?: number
  peak_concurrent?: number
  median_concurrent?: number
  [key: string]: number | undefined
}

/** How far back a run tested and how stale its data snapshot is. */
export interface CoverageFields {
  years: number | null
  data_age_days: number | null
}

/** Result if only `max_positions` trades could be open at once. */
export interface PositionCap {
  max_positions: number
  trades: number
  taken_pct?: number
  gross_bps: number
  net_bps: number
  win_rate?: number
  t_stat?: number
  sharpe?: number
}

/** Same names, same holding period, random entry days, costs deducted. */
export interface Baseline {
  method?: string
  draws_per_trade?: number
  trades?: number
  skipped?: number
  gross_bps: number
  net_bps: number
  trade_net_bps?: number
  excess_net_bps: number
  excess_t_stat?: number
}

export interface SplitMetrics {
  trades: number
  gross_bps: number
  net_bps: number
  win_rate?: number
  sharpe?: number
  t_stat?: number
}

export interface LatestRun {
  run_id: string
  date: string
  code_version: string
  parent_run_id: string | null
  change_note: string
  metrics: RunMetrics
  n_trials: number
  promoted: boolean
  oos_net_bps: number | null
  net_bps_cap10?: number | null
  baseline_excess_bps?: number | null
  /**
   * Coverage fields are optional on purpose: an app server started before the backend
   * gained them still answers with the older shape, and the page must degrade to n/a.
   */
  period?: { start: string; end: string }
  data?: { source: string; as_of: string; universe: string; bar_size: string }
  years?: number | null
  data_age_days?: number | null
  sparkline: number[]
}

export interface IdeaCard {
  id: string
  title: string
  status: string
  verdict: Verdict
  category: string
  horizon: string
  summary: string
  result?: string
  revisit_when?: string
  last_reviewed?: string
  tags?: string[]
  source?: { type?: string; author?: string; ref?: string }
  run_count: number
  latest_run: LatestRun | null
}

export interface ResultsSummary {
  total_ideas: number
  total_runs: number
  ideas_with_runs: number
  by_status: Record<string, number>
  by_verdict: Record<string, number>
}

export type SeriesPoint = [string, number]

export interface MonthlyRow {
  month: string
  n: number
  total_r: number
  net_r: number
}

export interface CostSensitivityPoint {
  round_trip_bps: number
  net_bps: number
}

export interface RunRecord {
  run_id: string
  date: string
  code_version: string
  data: { source: string; as_of: string; universe: string; bar_size: string }
  period: { start: string; end: string }
  costs: { model: string; round_trip_bps: number }
  metrics: RunMetrics
  params?: Record<string, unknown>
  parent_run_id?: string | null
  change_note?: string
  n_trials?: number
  promoted?: boolean
  cost_sensitivity?: CostSensitivityPoint[]
  position_caps?: PositionCap[]
  baseline?: Baseline
  years?: number | null
  data_age_days?: number | null
  split?: { type: string; oos_start: string }
  in_sample?: SplitMetrics
  out_of_sample?: SplitMetrics
  series?: {
    equity_r?: SeriesPoint[]
    drawdown_r?: SeriesPoint[]
    monthly?: MonthlyRow[]
  }
  validation?: string[]
  notes?: string
}

export interface MetricDelta {
  a: number | null
  b: number | null
  delta: number | null
}

export interface RunRef {
  run_id: string
  date: string
  code_version: string
  change_note?: string
}

export interface ParamDiff {
  changed: Record<string, { a: unknown; b: unknown }>
  added: Record<string, unknown>
  removed: Record<string, unknown>
}

interface Envelope {
  status: 'success' | 'error'
  message?: string
}

export interface BacktestListResponse extends Envelope {
  available?: boolean
  ideas?: IdeaCard[]
  summary?: ResultsSummary
}

export interface BacktestIdeaResponse extends Envelope {
  idea?: Omit<IdeaCard, 'run_count' | 'latest_run'>
  runs?: RunRecord[]
}

export interface BacktestCompareResponse extends Envelope {
  idea_id?: string
  run_a?: RunRef
  run_b?: RunRef
  metric_deltas?: Record<string, MetricDelta>
  params?: ParamDiff
  n_trials?: { a: number; b: number }
  comparable?: boolean
  warnings?: string[]
  info?: string[]
  equity_r?: { a: SeriesPoint[]; b: SeriesPoint[] }
}

async function unwrap<T extends Envelope>(
  promise: Promise<{ data: T }>,
  fallbackMessage: string
): Promise<T> {
  try {
    const res = await promise
    return res.data
  } catch (error) {
    // 400/404/500 reject in axios, but the backend still sent a JSON error body.
    const axiosError = error as { response?: { data?: T } }
    if (axiosError.response?.data) {
      return axiosError.response.data
    }
    return { status: 'error', message: fallbackMessage } as T
  }
}

export const backtestResultsApi = {
  list: (): Promise<BacktestListResponse> =>
    unwrap(
      webClient.get<BacktestListResponse>('/api/backtest-results'),
      'Failed to load backtests'
    ),

  idea: (ideaId: string): Promise<BacktestIdeaResponse> =>
    unwrap(
      webClient.get<BacktestIdeaResponse>(`/api/backtest-results/${encodeURIComponent(ideaId)}`),
      'Failed to load backtest history'
    ),

  compare: (ideaId: string, runA: string, runB: string): Promise<BacktestCompareResponse> => {
    const params = new URLSearchParams({ a: runA, b: runB })
    return unwrap(
      webClient.get<BacktestCompareResponse>(
        `/api/backtest-results/${encodeURIComponent(ideaId)}/compare?${params.toString()}`
      ),
      'Failed to compare runs'
    )
  },
}
