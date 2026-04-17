import { useMutation } from '@tanstack/react-query'
import { apiClient } from '@/api/client'

export interface SizingRequest {
  apikey: string
  symbol?: string
  exchange?: string
  side?: 'BUY' | 'SELL'
  product?: 'MIS' | 'NRML' | 'CNC'
  entry_price: number
  stop_loss: number
  target?: number
  capital?: number | null
  sizing_mode: 'fixed_fractional' | 'pct_of_capital'
  risk_per_trade?: number
  pct_of_capital?: number | null
  slippage_factor?: number
  max_sl_pct_for_sizing?: number
  min_entry_price?: number
  max_entry_price?: number
}

export interface SizingData {
  quantity: number
  raw_quantity: number
  risk_amount: number
  risk_pct_of_capital: number
  reward_amount: number
  position_value: number
  rr_ratio: number
  sl_distance_pct: number
  skip_reason: string | null
  warnings: string[]
}

export interface SizingResponse {
  status: 'success' | 'error'
  message?: string
  data?: SizingData
}

async function calculateSizing(request: SizingRequest): Promise<SizingResponse> {
  const response = await apiClient.post<SizingResponse>('/sizing/', request)
  return response.data
}

export function useSizing() {
  return useMutation<SizingResponse, Error, SizingRequest>({
    mutationFn: calculateSizing,
  })
}
