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

export interface StrategyPnlResponse {
  status: 'success' | 'error'
  mode?: 'live' | 'analyze'
  strategies?: StrategyPnl[]
  count?: number
  message?: string
  data_health?: DataHealth
}

export type PerformancePeriod = '1d' | '7d' | '30d' | '90d' | 'ytd' | 'all'

export interface DailyPnlPoint {
  date: string
  pnl: number
}

export interface EquityPoint {
  date: string
  value: number
}

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
  expectancy_r?: number | null
  total_r?: number | null
  r_covered?: number
  r_total?: number
  by_symbol?: SymbolPnl[]
  trades?: PerformanceTrade[]
}

export interface SymbolPnl {
  symbol: string
  trades: number
  net_pnl: number
  win_rate: number
}

export interface PerformanceTrade {
  strategy: string
  symbol: string
  direction: 'LONG' | 'SHORT'
  closed_quantity: number
  entry_price: number
  exit_price: number
  realized_pnl: number
  closed_at: string | null
  cost: number | null
  r_multiple: number | null
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
}

export interface StrategyComparisonResponse {
  status: 'success' | 'error'
  mode?: 'live' | 'analyze'
  period?: PerformancePeriod
  strategies?: StrategyComparisonRow[]
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
    period: PerformancePeriod
  ): Promise<StrategyDailyPerformance> => {
    const params = new URLSearchParams({ period })
    if (strategy) params.set('strategy', strategy)
    return unwrap(
      webClient.get<StrategyDailyPerformance>(`/api/strategy-pnl/daily?${params.toString()}`),
      'Failed to fetch daily performance'
    )
  },

  compare: (period: PerformancePeriod): Promise<StrategyComparisonResponse> => {
    const params = new URLSearchParams({ period })
    return unwrap(
      webClient.get<StrategyComparisonResponse>(`/api/strategy-pnl/compare?${params.toString()}`),
      'Failed to fetch strategy comparison'
    )
  },
}
