import type { PerformancePeriod } from '@/api/strategyPnl'
import { webClient } from './client'

/** Query-string value for a filter; blank or absent means "all". */
export interface DiscretionaryFilters {
  broker?: string
  symbol?: string
  account?: string
}

export interface DiscretionaryRange {
  start: string
  end: string
}

export type PriceStatus =
  | 'live'
  | 'stale'
  | 'last_close'
  | 'cached'
  | 'unavailable'
  | 'not_requested'

export type ChargesStatus = 'ok' | 'unavailable'

/**
 * compute_metrics output; capital-derived ratios are null (see `withheld`). Money (net_profit,
 * equity_curve, daily_pnl, drawdown) sums every closing leg on its own date, so a partial exit
 * counts on its day; trades_count, wins / losses and every ratio are completed trades (a
 * position from flat to flat). With no completed trade the ratios are 0: show a dash.
 */
export interface DiscretionaryMetrics {
  start_date?: string
  end_date?: string
  /** Completed trades (a position from flat to flat), not closing legs. */
  trades_count: number
  winning_trades: number
  losing_trades: number
  /** Percent, 0-100. */
  win_rate: number
  gross_profit: number
  gross_loss: number
  /** GROSS realized P&L (before charges); not the net figure. */
  net_profit: number
  profit_factor: number | null
  expectancy: number
  avg_win: number
  avg_loss: number
  sharpe_ratio?: number | null
  sortino_ratio?: number | null
  return_pct?: number | null
  max_drawdown: number
  equity_basis?: string
  equity_curve?: { date: string; value: number }[]
  daily_pnl?: { date: string; pnl: number }[]
  withheld?: Record<string, string>
  [key: string]: unknown
}

export interface SyncHealth {
  last_successful_sync: string | null
  runs_by_day: Record<string, Record<string, number>>
  days_without_sync: string[]
}

/** Money of the legs closed in the period; trades / wins / losses of the completed trades whose
 * final close is in it (a day with only a partial exit has money and 0 trades). */
export interface PeriodRow {
  period: string
  label: string
  start: string
  end: string
  trades: number
  wins: number
  losses: number
  realized_gross: number
  charges: number
  charges_unavailable_trades: number
  net_of_charges: number
  net_charges_complete: boolean
}

export interface BreakdownRow {
  /** The group value; absent on the by_holding rows, which are named by their bucket. */
  key?: string
  trades: number
  wins: number
  losses: number
  /** Percent, 0-100. */
  win_rate: number
  /** Null when there is profit and no loss (or nothing to divide). */
  profit_factor: number | null
  expectancy: number
  avg_win: number
  avg_loss: number
  realized_gross: number
  charges: number
  charges_unavailable_trades: number
  net_of_charges: number
  net_charges_complete?: boolean
  avg_hold_minutes: number | null
  median_hold_minutes: number | null
}

export interface HoldStat {
  /** Trades with a hold time that went into the figures. */
  count: number
  mean_minutes: number | null
  median_minutes: number | null
}

export interface HoldSummary {
  overall: HoldStat
  winners: HoldStat
  losers: HoldStat
}

/**
 * A COMPLETED trade: one position from flat to flat (a reversal ends it); partial exits are its
 * legs. `id` is the position id within the build; quantity is the total closed, entry / exit the
 * quantity-weighted averages, P&L and charges the sums of the legs, hold from the opening to the
 * final close, and trade_date the session date of the final close.
 */
export interface DiscretionaryTrade {
  id: number
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  direction: 'LONG' | 'SHORT'
  closed_quantity: number
  entry_price: number
  exit_price: number
  opened_at: string
  closed_at: string
  trade_date: string
  overnight: boolean
  hold_minutes: number | null
  realized_pnl: number
  charges: number | null
  charges_status: ChargesStatus
  net_pnl: number | null
  /** Number of closing legs (partial exits) in this trade. */
  legs: number
  position_seq?: number
}

export interface DiscretionaryPosition {
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  direction: 'LONG' | 'SHORT'
  open_qty: number
  avg_entry: number
  opened_at: string
  ltp: number | null
  fetched_at: string | null
  age_s: number | null
  price_status: PriceStatus
  unrealized: number | null
  /** Gross P&L of this position's partial exits so far (already in realized). */
  realized_so_far?: number | null
  opening_unknown?: boolean
}

export interface PerIdentityRow {
  broker: string
  account: string
  exchange: string
  symbol: string
  product: string
  /** Completed trades; the money is every leg of the identity in the period. */
  trades: number
  realized_gross: number
  charges: number
  charges_unavailable_trades: number
  net: number
  unrealized: number
  unrealized_unavailable_positions: number
}

export interface DiscretionarySummary {
  period: { period: string; start: string; end: string; reliable_since?: string }
  metrics: DiscretionaryMetrics
  build_id: string | null
  realized_gross: number
  charges: number
  net: number
  realized_net_of_charges: number
  net_definition?: string
  charges_unavailable_trades: number
  unrealized_total: number
  open_positions: number
  per_identity: PerIdentityRow[]
  sync_health: SyncHealth
  warnings: string[]
  market_hours_source?: 'calendar' | 'clock' | 'mixed' | string
  withheld: Record<string, string>
  labels?: Record<string, string>
  quotes_requested: boolean
  by_period: { daily: PeriodRow[]; weekly: PeriodRow[]; monthly: PeriodRow[] }
  by_symbol: BreakdownRow[]
  by_side: BreakdownRow[]
  by_product: BreakdownRow[]
  by_holding: { intraday: BreakdownRow; overnight: BreakdownRow }
  hold: HoldSummary
  trades: DiscretionaryTrade[]
  trades_total: number
  trades_truncated: number
  positions: DiscretionaryPosition[]
  positions_total: number
  positions_truncated: number
}

export interface TradeFill {
  fill_ref: number
  role: 'ENTRY' | 'EXIT'
  qty_used: number
  charge_allocated: number | null
  price: number
  fill_time: string | null
  order_id: string
  fill_id: string | null
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  side: string
  attribution_state: string
}

export interface TradeFills {
  build_id: string | null
  trade: DiscretionaryTrade
  /** The fills of EVERY leg, one row per (fill, role): entry fills first, then exits. */
  fills: TradeFill[]
  /** Number of fill-to-leg links in the build, before merging per fill. */
  linked_fills: number
}

/** A ledger fill that could not be attributed, or one the book could not place. */
export interface UnresolvedFill {
  id?: number
  fill_ref?: number | null
  reason?: string
  broker?: string | null
  account?: string | null
  symbol?: string | null
  exchange?: string | null
  product?: string | null
  side?: string | null
  qty?: number
  price?: number | null
  fill_time?: string | null
  order_id?: string
  attribution_state?: string
}

export interface UnresolvedResponse {
  unresolved: UnresolvedFill[]
  unresolved_count: number
  orphans: UnresolvedFill[]
  orphans_count: number
}

/** An error from the Discretionary API, with the backend's machine code when it sent one. */
export class DiscretionaryApiError extends Error {
  code?: string
  httpStatus?: number

  constructor(message: string, code?: string, httpStatus?: number) {
    super(message)
    this.name = 'DiscretionaryApiError'
    this.code = code
    this.httpStatus = httpStatus
  }
}

function buildParams(
  filters: DiscretionaryFilters,
  extra: Record<string, string | undefined> = {}
): URLSearchParams {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries({ ...extra, ...filters })) {
    const text = (value ?? '').trim()
    if (text) params.set(key, text)
  }
  return params
}

export function summaryParams(
  period: PerformancePeriod,
  range: DiscretionaryRange | undefined,
  filters: DiscretionaryFilters,
  quotes: boolean
): URLSearchParams {
  const params = buildParams(filters, { period, quotes: quotes ? '1' : '0' })
  if (period === 'custom' && range) {
    params.set('start', range.start)
    params.set('end', range.end)
  }
  return params
}

async function read<T extends { status?: string }>(
  promise: Promise<{ data: T }>,
  fallbackMessage: string
): Promise<T> {
  try {
    const res = await promise
    if (res.data?.status === 'error') {
      const body = res.data as { message?: string; code?: string }
      throw new DiscretionaryApiError(body.message || fallbackMessage, body.code)
    }
    return res.data
  } catch (error) {
    if (error instanceof DiscretionaryApiError) throw error
    // Error responses (400/404/500) are non-2xx, so axios rejects: recover the JSON body.
    const axiosError = error as {
      response?: { status?: number; data?: { message?: string; code?: string } }
    }
    const body = axiosError.response?.data
    throw new DiscretionaryApiError(
      body?.message || fallbackMessage,
      body?.code,
      axiosError.response?.status
    )
  }
}

export const discretionaryPnlApi = {
  /**
   * Whether the Discretionary source is enabled on the server. Never throws: a 404 from an older
   * backend, a network error or a disabled flag all mean "not available", so the page behaves
   * exactly as it did before this feature.
   */
  status: async (): Promise<boolean> => {
    try {
      const res = await webClient.get<{ status?: string; enabled?: boolean }>(
        '/api/discretionary-pnl/status'
      )
      return res.data?.status === 'success' && res.data.enabled === true
    } catch {
      return false
    }
  },

  summary: (
    period: PerformancePeriod,
    range: DiscretionaryRange | undefined,
    filters: DiscretionaryFilters,
    quotes: boolean
  ): Promise<DiscretionarySummary> =>
    read(
      webClient.get<DiscretionarySummary & { status?: string }>(
        `/api/discretionary-pnl/summary?${summaryParams(period, range, filters, quotes).toString()}`
      ),
      'Could not load the discretionary P&L'
    ),

  /**
   * The fills behind one closed trade. Trade ids are only unique within a build, so the build_id
   * of the summary the trade came from is required: the server answers 409 `build_changed` when
   * the data was rebuilt since.
   */
  tradeFills: (tradeId: number, buildId: string): Promise<TradeFills> =>
    read(
      webClient.get<TradeFills & { status?: string }>(
        `/api/discretionary-pnl/trades/${encodeURIComponent(String(tradeId))}/fills?${new URLSearchParams({ build_id: buildId }).toString()}`
      ),
      'Could not load the fills of this trade'
    ),

  unresolved: (filters: DiscretionaryFilters): Promise<UnresolvedResponse> =>
    read(
      webClient.get<UnresolvedResponse & { status?: string }>(
        `/api/discretionary-pnl/unresolved?${buildParams(filters).toString()}`
      ),
      'Could not load the unresolved fills'
    ),
}
