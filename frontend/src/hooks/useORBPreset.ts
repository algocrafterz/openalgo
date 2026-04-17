import { useMutation } from '@tanstack/react-query'
import axios from 'axios'

export interface ORBPresetRequest {
  apikey: string
  symbol: string
  exchange: string
  orb_minutes?: number
  tp_rr?: number
  capital?: number | null
}

export interface ORBLevels {
  orb_high: number
  orb_low: number
  orb_range: number
  orb_minutes: number
  ltp: number
  side: 'BUY' | 'SELL' | 'INSIDE'
  candles_used: number
}

export interface ORBPresetInputs {
  symbol: string
  exchange: string
  side: string
  product: string
  entry_price: number
  stop_loss: number
  target: number
  sizing_mode: string
  risk_per_trade: number
  slippage_factor: number
}

export interface ORBSizingResult {
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

export interface ORBPresetResponse {
  status: 'success' | 'error'
  message?: string
  data?: {
    orb: ORBLevels
    inputs: ORBPresetInputs
    sizing: ORBSizingResult
  }
}

async function fetchORBPreset(req: ORBPresetRequest): Promise<ORBPresetResponse> {
  const { data } = await axios.post<ORBPresetResponse>('/api/v1/sizing/orb', req)
  return data
}

export function useORBPreset() {
  return useMutation<ORBPresetResponse, Error, ORBPresetRequest>({
    mutationFn: fetchORBPreset,
  })
}
