import { webClient } from './client'

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
}
