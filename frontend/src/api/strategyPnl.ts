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

function periodParams(period: PerformancePeriod, range?: DateRange): URLSearchParams {
  const params = new URLSearchParams({ period })
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
  | 'too few trades'
  | 'too few days'
  | 'likely real'
  | 'likely negative'
  | 'not distinguishable from zero'

export interface StrategyDailyPerformance {
  status: 'success' | 'error'
  mode?: 'live' | 'analyze'
  strategy?: string | null
  period?: PerformancePeriod
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
  sharpe_ratio?: number
  sortino_ratio?: number
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
  /** Per-trade net (after estimated charges) edge statistics. */
  net_expectancy?: number | null
  net_pnl_std?: number | null
  t_stat?: number | null
  expectancy_ci95?: [number, number] | null
  edge_verdict?: EdgeVerdict
  edge_days?: number | null
  day_t_stat?: number | null
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
  sharpe_ratio: number
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
}

export interface StrategyComparisonResponse {
  status: 'success' | 'error'
  mode?: 'live' | 'analyze'
  period?: PerformancePeriod
  start_date?: string
  end_date?: string
  strategies?: StrategyComparisonRow[]
  all_strategies?: string[]
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
    strategy: string | null,
    period: PerformancePeriod,
    range?: DateRange
  ): Promise<StrategyDailyPerformance> => {
    const params = periodParams(period, range)
    if (strategy) params.set('strategy', strategy)
    return unwrap(
      webClient.get<StrategyDailyPerformance>(`/api/strategy-pnl/daily?${params.toString()}`),
      'Failed to fetch daily performance'
    )
  },

  compare: (period: PerformancePeriod, range?: DateRange): Promise<StrategyComparisonResponse> => {
    const params = periodParams(period, range)
    return unwrap(
      webClient.get<StrategyComparisonResponse>(`/api/strategy-pnl/compare?${params.toString()}`),
      'Failed to fetch strategy comparison'
    )
  },

  /** Downloads the closed trades of a range as CSV; throws an Error with a trader-readable message. */
  exportTrades: async (
    strategy: string | null,
    period: PerformancePeriod,
    range?: DateRange
  ): Promise<{ blob: Blob; filename: string }> => {
    const params = periodParams(period, range)
    if (strategy) params.set('strategy', strategy)
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
