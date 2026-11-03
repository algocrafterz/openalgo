import { type StrategyFilter, strategyFilterParams } from '@/lib/strategyFilter'
import { webClient } from './client'

export interface DataHealthMismatch {
  symbol: string
  exchange: string
  product: string
  book_quantity: number
  broker_quantity: number
  strategies: string[]
}

export interface DataHealth {
  status: 'ok' | 'stale' | 'unverified'
  warnings: string[]
  mismatches: DataHealthMismatch[]
  recovered_fills: number
  last_reconciled_at: string | null
  feed_connected: boolean
}

export interface StrategyLeg {
  symbol: string
  exchange: string
  product: string
  quantity: number
  average_price: number
  ltp: number | null
  realized: number
  today_realized: number
  unrealized: number
}

export interface StrategyPnl {
  strategy: string
  realized: number
  today_realized: number
  unrealized: number
  total: number
  today_total: number
  open_quantity: number
  unpriced_legs: number
  legs: StrategyLeg[]
}

export interface LastSession {
  date: string
  strategies: Record<string, number>
  total: number
  trades_count: number
}

export interface StrategyPnlResponse {
  status: 'success' | 'error'
  mode?: 'live' | 'analyze'
  strategies?: StrategyPnl[]
  count?: number
  message?: string
  data_health?: DataHealth
  last_session?: LastSession | null
}

export type PerformancePeriod = '1d' | '7d' | '15d' | '30d' | '90d' | 'ytd' | 'all' | 'custom'

/** Inclusive YYYY-MM-DD bounds, used only with the 'custom' period. */
export interface DateRange {
  start: string
  end: string
}

/** A strategy filter, or one strategy name (or null for all) as older callers pass it. */
export type StrategySelection = string | null | StrategyFilter

const NOTHING_SELECTED = 'Select at least one strategy.'

/** True when the selection is a filter the user cleared on purpose: never a whole-portfolio request. */
function isNoneSelection(selection?: StrategySelection): boolean {
  return typeof selection === 'object' && selection !== null && selection.none === true
}

function periodParams(
  period: PerformancePeriod,
  range?: DateRange,
  selection?: StrategySelection,
  capital?: number | null
): URLSearchParams {
  const params = new URLSearchParams({ period })
  if (capital !== undefined && capital !== null) params.set('capital', String(capital))
  if (selection) {
    const filter = typeof selection === 'string' ? { include: [selection], exclude: [] } : selection
    for (const [key, value] of strategyFilterParams(filter)) params.append(key, value)
  }
  if (period === 'custom' && range) {
    params.set('start', range.start)
    params.set('end', range.end)
  }
  return params
}

export interface DailyPnlPoint {
  date: string
  pnl: number
}

export interface EquityPoint {
  date: string
  value: number
}

export type EdgeVerdict =
  | 'insufficient_sample'
  | 'positive_supported'
  | 'positive_inconclusive'
  | 'negative'
  | 'inconclusive'

/** Recorded rupee metrics scaled to a capital per strategy (daily view only). */
export interface CapitalViewScaled {
  net_profit: number | null
  net_after_costs: number | null
  est_costs: number | null
  max_drawdown: number | null
  best_day: number | null
  worst_day: number | null
}

/** A straight-line what-if at the capital per strategy the user entered. */
export interface CapitalView {
  /** Capital per strategy, as entered. */
  capital: number
  /** Strategies that contributed trades to the selection. */
  strategies: number
  /** capital x strategies. */
  total_capital: number | null
  /** Capital per strategy the recorded trades are taken as sized on. */
  reference_capital: number
  /** capital / reference_capital. */
  scale: number | null
  scaled: CapitalViewScaled
  /** Gross realised P&L over the total capital. */
  return_pct: number | null
  /** Realised P&L after estimated charges over the total capital. */
  return_net_pct: number | null
  /** Max drawdown over the total capital (zero or negative). */
  max_drawdown_pct_of_capital: number | null
  best_day_pct: number | null
  worst_day_pct: number | null
  basis: Record<string, string>
}

export type Grade = 'poor' | 'acceptable' | 'good' | 'excellent'

export interface StrategyConcentration {
  trades: number
  net: number
  share_of_net_pct: number | null
  best_day_net: number
  best_day_share_of_own_pct: number | null
  net_ex_best_day: number
  net_ex_portfolio_best_day: number
}

export interface ConcentrationStats {
  traded_days: number
  trades: number
  net?: number
  best_day?: number
  worst_day?: number
  mean_daily?: number
  median_daily?: number
  profitable_days?: number
  profitable_days_pct?: number | null
  avg_winning_day?: number | null
  avg_losing_day?: number | null
  net_ex_best_day?: number
  net_ex_best_five_days?: number | null
  top1_days_share_pct?: number | null
  top3_days_share_pct?: number | null
  top5_days_share_pct?: number | null
  top10pct_trades?: number
  top10pct_trades_share_of_net_pct?: number | null
  best_day_date?: string
  by_strategy?: Record<string, StrategyConcentration>
}

export interface NetDrawdown {
  basis: string
  net: number
  capital_base: number
  return_on_capital_pct: number
  max_drawdown: number
  max_drawdown_pct: number
  recovery_factor: number | null
  drawdown_peak_date?: string | null
  drawdown_trough_date?: string
  drawdown_duration_sessions?: number
  drawdown_recovered?: boolean
  calmar_ratio: number | null
  max_drawdown_trade_sequence: number
  recovery_factor_trade_sequence: number | null
}

export interface BucketRow {
  label: string | number
  trades: number
  win_rate: number | null
  net: number
  profit_factor: number | null
  expectancy: number
  expectancy_r: number | null
  r_trades: number
}

export interface RollingPoint {
  n: number
  date: string
  exp20: number | null
  pf50: number | null
}

export interface SlippageBlock {
  trades: number
  net: number
  adverse_total: number
  favourable_total: number
  adverse_trades: number
  favourable_trades: number
  net_bps: number | null
  adverse_bps: number | null
}

export interface GradeMetric {
  metric_value: number | null
  grade: Grade | null
  reason: string | null
}

export interface GradingReport {
  metrics: Record<string, GradeMetric>
  overall: Grade | null
  /** Weakest return metric when the risk metrics cannot yet be graded; not a proven edge. */
  preliminary: Grade | null
  assessment: 'validated' | 'preliminary' | 'none'
  overall_note: string | null
  sample: { trades: number; sessions: number }
}

export interface PerformanceInsights {
  concentration: ConcentrationStats
  drawdown: NetDrawdown
  streaks: {
    max_consecutive_losses: number
    avg_losing_streak: number | null
    losing_streaks: number
  }
  gross_profit_factor: number | null
  net_profit_factor: number | null
  grading: GradingReport
  deployment: {
    avg_daily_peak_deployed: number | null
    max_deployed: number | null
    deployment_days: number
    deployment_excluded: number
    deployment_basis: string
  }
  rolling: {
    combined: RollingPoint[]
    by_strategy: Record<string, RollingPoint[]>
    expectancy_window: number
    pf_window: number
  }
  monthly: {
    month: string
    trades: number
    traded_days: number
    net: number
    win_rate: number | null
    profit_factor: number | null
    profitable_days_pct: number | null
  }[]
  entry_quality: {
    by_hour: BucketRow[]
    by_weekday: BucketRow[]
    by_direction: BucketRow[]
    by_session_part: BucketRow[]
    entry_excluded: number
    first_hour_ends: string
  }
  slippage: Partial<SlippageBlock> & {
    covered: number
    total: number
    basis: string
    by_strategy: Record<string, SlippageBlock>
    by_symbol: (SlippageBlock & { symbol: string })[]
  }
}

export interface ExcursionBlock {
  trades: number
  r_trades: number
  avg_mfe_r: number | null
  avg_mae_r: number | null
  mfe_r_quartiles: number[] | null
  mfe_capture_pct: number | null
  winners_avg_mae_r: number | null
  losers_avg_mae_r: number | null
  mae_reached_stop_pct: number | null
  reached_meaningful_mfe: number
  gave_back_pct: number | null
}

export interface ExcursionReport extends ExcursionBlock {
  total: number
  covered: number
  excluded_flagged: number
  meaningful_mfe_r: number
  basis: string
  by_strategy: Record<string, ExcursionBlock>
}

export interface ExcursionResponse {
  status: 'success' | 'error'
  excursions: ExcursionReport | null
  message?: string
}

export interface StrategyDailyPerformance {
  status: 'success' | 'error'
  mode?: 'live' | 'analyze'
  strategy?: string | null
  period?: PerformancePeriod
  /** Strategies that contributed trades in the window, sorted. */
  included?: string[]
  /** The exclude list as requested, sorted. */
  excluded?: string[]
  message?: string
  data_health?: DataHealth
  start_date?: string
  end_date?: string
  trades_count?: number
  winning_trades?: number
  losing_trades?: number
  win_rate?: number
  gross_profit?: number
  gross_loss?: number
  net_profit?: number
  profit_factor?: number | null
  expectancy?: number
  avg_win?: number
  avg_loss?: number
  payoff_ratio?: number | null
  sharpe_ratio?: number | null
  sortino_ratio?: number | null
  calmar_ratio?: number | null
  max_drawdown?: number
  ulcer_index?: number
  recovery_factor?: number | null
  best_win_streak?: number
  best_loss_streak?: number
  best_day?: number
  worst_day?: number
  trading_days?: number
  calendar_days?: number
  daily_pnl?: DailyPnlPoint[]
  equity_curve?: EquityPoint[]
  est_costs?: number | null
  net_after_costs?: number | null
  /** Estimated charges as a percent of gross profit; null when there is no gross profit. */
  charges_pct_of_gross?: number | null
  /** Gross traded value (entry plus exit) the charges were estimated on. */
  turnover?: number
  charges_pct_of_turnover?: number | null
  avg_charge_per_trade?: number | null
  by_broker?: BrokerCharges[]
  /** True when any trade was priced on an assumed fee schedule rather than its broker's own. */
  fee_basis_assumed?: boolean
  expectancy_r?: number | null
  total_r?: number | null
  r_covered?: number
  r_total?: number
  /** Expectancy in R after estimated charges (expectancy_r is before charges). */
  expectancy_net_r?: number | null
  total_net_r?: number | null
  net_r_covered?: number
  /** Trades without an R multiple, counted by reason. */
  r_excluded?: Record<string, number>
  /** Withheld (null) until the range has enough sessions; see ratio_warning. */
  ratio_sessions?: number
  ratio_losing_sessions?: number
  /** Assumed allocated capital behind the daily returns, not measured deployed capital. */
  ratio_capital?: number
  ratio_basis?: string
  ratio_warning?: string | null
  /** Per-trade net (after estimated charges) edge statistics. */
  net_expectancy?: number | null
  net_pnl_std?: number | null
  t_stat?: number | null
  expectancy_ci90?: [number, number] | null
  expectancy_ci95?: [number, number] | null
  /** Day-clustered bootstrap intervals for the per-trade average. */
  bootstrap_ci90?: [number, number] | null
  bootstrap_ci95?: [number, number] | null
  bootstrap_p_nonpositive?: number | null
  bootstrap_resamples?: number | null
  edge_verdict?: EdgeVerdict
  edge_days?: number | null
  day_t_stat?: number | null
  insights?: PerformanceInsights
  edge_trades?: number
  /** Trades whose charges could be estimated; the rest count as zero charges. */
  edge_cost_coverage?: number
  /** Measured entry slippage in rupees (fill against signal price); already inside the P&L. */
  entry_slippage?: number | null
  entry_slippage_bps?: number | null
  slippage_covered?: number
  slippage_total?: number
  by_symbol?: SymbolPnl[]
  /** Trades bucketed by EXIT hour (entry time is not recorded). */
  by_hour?: HourPnl[]
  by_weekday?: WeekdayPnl[]
  hold_stats?: HoldStats
  by_entry_hour?: EntryHourPnl[]
  by_hold_bucket?: HoldBucketPnl[]
  /** Cumulative P&L after estimated charges, on the same dates as equity_curve. */
  equity_curve_net?: EquityPoint[]
  equity_net_cost_coverage?: { covered: number; total: number }
  /** Single-day views only: trades with a measured MFE/MAE. */
  excursion_covered?: number
  excursion_total?: number
  trades?: PerformanceTrade[]
  /** Present only when a capital was sent with the request. */
  capital_view?: CapitalView
}

export interface HourPnl {
  hour: number
  trades: number
  net_pnl: number
  win_rate: number
}

export interface HoldStats {
  avg_min: number | null
  median_min: number | null
  p90_min: number | null
  winners_avg_min: number | null
  losers_avg_min: number | null
  /** Trades with a recorded entry time, out of total. */
  covered: number
  total: number
  /** Trades left out of the holding-time figures because their timestamps are unreliable. */
  excluded?: Record<string, number>
}

export interface EntryHourPnl extends HourPnl {
  avg_hold_min: number | null
}

export interface HoldBucketPnl {
  bucket: string
  trades: number
  net_pnl: number
  win_rate: number
  avg_pnl: number
}

export interface WeekdayPnl {
  weekday: string
  trades: number
  net_pnl: number
  win_rate: number
}

export interface SymbolPnl {
  symbol: string
  trades: number
  net_pnl: number
  win_rate: number
}

/** Estimated charges for the trades one broker placed, priced on that broker's own fees. */
export interface BrokerCharges {
  broker: string
  label: string
  /** True when these rates are a stand-in, not read from the broker's own published page. */
  assumed: boolean
  source: string
  /** Why these charges need a look (assumed rates, or fees read long ago); null when fine. */
  attention?: string | null
  trades: number
  gross: number
  est_costs: number | null
  net_after_costs: number | null
  charges_pct_of_gross: number | null
}

export interface PerformanceTrade {
  strategy: string
  broker?: string
  symbol: string
  direction: 'LONG' | 'SHORT'
  closed_quantity: number
  entry_price: number
  exit_price: number
  realized_pnl: number
  closed_at: string | null
  /** ISO, naive IST; null when the entry time was not recorded. */
  opened_at?: string | null
  hold_min?: number | null
  cost: number | null
  r_multiple: number | null
  /** Rupees, positive; upper-bound estimate from 1-minute bars (single-day views only). */
  mfe?: number | null
  mae?: number | null
}

export interface StrategyComparisonRow {
  strategy: string
  trades_count: number
  win_rate: number
  profit_factor: number | null
  net_profit: number
  expectancy: number
  sharpe_ratio: number | null
  sortino_ratio?: number | null
  ratio_warning?: string | null
  max_drawdown: number
  best_win_streak: number
  best_loss_streak: number
  avg_hold_min?: number | null
  median_hold_min?: number | null
  est_costs?: number | null
  net_after_costs?: number | null
  charges_pct_of_gross?: number | null
  net_expectancy?: number | null
  t_stat?: number | null
  edge_verdict?: EdgeVerdict
  gross_profit_factor?: number | null
  net_profit_factor?: number | null
  expectancy_net_r?: number | null
  return_on_capital_pct?: number | null
  max_drawdown_pct?: number | null
  recovery_factor?: number | null
  top1_day_share_pct?: number | null
  top5_days_share_pct?: number | null
  overall_grade?: Grade | null
  preliminary_grade?: Grade | null
  overall_note?: string | null
}

export interface ComparisonPortfolio {
  net: number
  net_profit_factor: number | null
  return_on_capital_pct: number
  max_drawdown: number
  max_drawdown_pct: number
  recovery_factor: number | null
  overall_grade: Grade | null
  preliminary_grade: Grade | null
  overall_note: string | null
}

export interface ContributionRow {
  strategy: string
  net: number
  share_of_net_pct: number | null
  portfolio_dd_with: number
  portfolio_dd_without: number
  drawdown_change: number
}

export interface StrategyComparisonResponse {
  status: 'success' | 'error'
  mode?: 'live' | 'analyze'
  period?: PerformancePeriod
  start_date?: string
  end_date?: string
  strategies?: StrategyComparisonRow[]
  portfolio?: ComparisonPortfolio | null
  correlation?: { strategies: string[]; matrix: (number | null)[][]; sessions: number } | null
  contribution?: ContributionRow[]
  /** Every strategy with trades, whatever the filter, so an excluded one can be added back. */
  all_strategies?: string[]
  included?: string[]
  excluded?: string[]
  message?: string
  data_health?: DataHealth
}

async function unwrap<T extends { status: 'success' | 'error'; message?: string }>(
  promise: Promise<{ data: T }>,
  fallbackMessage: string
): Promise<T> {
  try {
    const res = await promise
    return res.data
  } catch (error) {
    // Error responses (400/503/502) are non-2xx, so axios rejects instead of
    // resolving - recover the JSON body the backend still sent.
    const axiosError = error as { response?: { data?: T } }
    if (axiosError.response?.data) {
      return axiosError.response.data
    }
    return { status: 'error', message: fallbackMessage } as T
  }
}

export const strategyPnlApi = {
  get: async (): Promise<StrategyPnlResponse> => {
    try {
      const res = await webClient.get<StrategyPnlResponse>('/api/strategy-pnl')
      return res.data
    } catch (error) {
      // Error responses (400/503/502) are non-2xx, so axios rejects instead of
      // resolving - recover the JSON body the backend still sent.
      const axiosError = error as { response?: { data?: StrategyPnlResponse } }
      if (axiosError.response?.data) {
        return axiosError.response.data
      }
      return { status: 'error', message: 'Failed to fetch strategy P&L' }
    }
  },

  daily: (
    strategy: StrategySelection,
    period: PerformancePeriod,
    range?: DateRange,
    capital?: number | null
  ): Promise<StrategyDailyPerformance> => {
    if (isNoneSelection(strategy)) return Promise.reject(new Error(NOTHING_SELECTED))
    const params = periodParams(period, range, strategy, capital)
    return unwrap(
      webClient.get<StrategyDailyPerformance>(`/api/strategy-pnl/daily?${params.toString()}`),
      'Failed to fetch daily performance'
    )
  },

  compare: (
    period: PerformancePeriod,
    range?: DateRange,
    filter?: StrategyFilter
  ): Promise<StrategyComparisonResponse> => {
    if (isNoneSelection(filter)) return Promise.reject(new Error(NOTHING_SELECTED))
    const params = periodParams(period, range, filter)
    return unwrap(
      webClient.get<StrategyComparisonResponse>(`/api/strategy-pnl/compare?${params.toString()}`),
      'Failed to fetch strategy comparison'
    )
  },

  excursions: (
    strategy: StrategySelection,
    period: PerformancePeriod,
    range?: DateRange
  ): Promise<ExcursionResponse> => {
    if (isNoneSelection(strategy)) return Promise.reject(new Error(NOTHING_SELECTED))
    const params = periodParams(period, range, strategy)
    return unwrap(
      webClient.get<ExcursionResponse>(`/api/strategy-pnl/daily/excursions?${params.toString()}`),
      'Failed to fetch excursion quality'
    )
  },

  /** Downloads the closed trades of a range as CSV; throws an Error with a trader-readable message. */
  exportTrades: async (
    strategy: StrategySelection,
    period: PerformancePeriod,
    range?: DateRange
  ): Promise<{ blob: Blob; filename: string }> => {
    if (isNoneSelection(strategy)) throw new Error(NOTHING_SELECTED)
    const params = periodParams(period, range, strategy)
    try {
      const res = await webClient.get<Blob>(
        `/api/strategy-pnl/trades/export?${params.toString()}`,
        {
          responseType: 'blob',
        }
      )
      const disposition = String(res.headers['content-disposition'] ?? '')
      const match = /filename="?([^";]+)"?/.exec(disposition)
      return { blob: res.data, filename: match?.[1] ?? 'strategy-trades.csv' }
    } catch (error) {
      throw new Error(await exportErrorMessage(error))
    }
  },
}

async function exportErrorMessage(error: unknown): Promise<string> {
  const fallback = 'Could not export the trades. Please try again.'
  const data = (error as { response?: { data?: unknown } }).response?.data
  if (data instanceof Blob) {
    try {
      const body = JSON.parse(await data.text()) as { message?: string }
      return body.message || fallback
    } catch {
      return fallback
    }
  }
  return fallback
}
