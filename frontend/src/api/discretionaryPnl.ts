import type { PerformancePeriod } from '@/api/strategyPnl'
import { webClient } from './client'

/** Trade category filter; unclassified positions are part of 'all' only. */
export type DiscretionaryCategory = 'all' | 'intraday' | 'swing'

export const CATEGORY_CHOICES: { value: DiscretionaryCategory; label: string }[] = [
  { value: 'all', label: 'All trades' },
  { value: 'intraday', label: 'Intraday' },
  { value: 'swing', label: 'Swing' },
]

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
  /** Completed trades with a net P&L (the win / loss basis) and those without one. */
  net_basis_trades?: number
  net_unavailable_trades?: number
  winning_trades: number
  losing_trades: number
  /** Percent, 0-100. Null (with `count_unavailable_reason`) when no completed trade has a net P&L. */
  win_rate: number | null
  gross_profit: number
  gross_loss: number
  /** GROSS realized P&L (before charges); not the net figure. */
  net_profit: number
  profit_factor: number | null
  expectancy: number | null
  avg_win: number | null
  avg_loss: number | null
  /** Why the ratios above are null (no completed trade has a net P&L); null otherwise. */
  count_unavailable_reason?: string | null
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
  /** Percent, 0-100. Null when no position of the group has a net P&L. */
  win_rate: number | null
  /** Null when there is profit and no loss (or nothing to divide). */
  profit_factor: number | null
  expectancy: number | null
  avg_win: number | null
  avg_loss: number | null
  /** Why the ratios are null (no position with a net P&L); null otherwise. */
  stats_unavailable_reason?: string | null
  realized_gross: number
  charges: number
  charges_unavailable_trades: number
  net_of_charges: number
  net_charges_complete?: boolean
  avg_hold_minutes: number | null
  median_hold_minutes: number | null
  /** "How you trade" rows only: distinct session dates of the row's completed trades. */
  trading_days?: number
  /** "How you trade" rows only: the row meets the edge-check sample rule (show its profit factor). */
  sample_ok?: boolean
  /** The row is below the edge-check sample rule: show its profit factor with a marker. */
  low_sample?: boolean
}

/** Sample-size context of a set of completed trades (the existing edge-check rule). */
export interface SampleContext {
  completed_trades: number
  trading_days: number
  min_trades: number
  min_days: number
  sufficient: boolean
  rule?: string
}

/** A completed trade named by the risk strip; `id` + `build_id` open the fills drill-down. */
export interface TradeRef {
  id: number
  build_id?: string | null
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  direction: 'LONG' | 'SHORT'
  realized_pnl: number
  charges: number | null
  charges_status?: ChargesStatus | null
  net_pnl: number | null
  closed_at: string
  trade_date: string
}

/** Risk and consistency strip; gross realized unless the name says net. */
export interface ConsistencyBlock {
  basis?: string
  completed_trades: number
  largest_win: TradeRef | null
  largest_loss: TradeRef | null
  max_consecutive_wins: number
  max_consecutive_losses: number
  streak_rule?: string
  trading_days: number
  profitable_days: number
  losing_days: number
  flat_days: number
  /** Null (with the reason) below 2 trading days. */
  avg_daily_pnl: number | null
  avg_daily_net: number | null
  avg_daily_net_complete: boolean
  avg_daily_unavailable_reason: string | null
  max_drawdown: number | null
  max_drawdown_unavailable_reason: string | null
  drawdown_basis?: string
  avg_hold_minutes: number | null
  median_hold_minutes: number | null
  /** Additive keys: each is ABSENT when it cannot be computed (older server, too little data). */
  net_days?: NetDayStats
  trades_per_day?: TradesPerDay
  reentry_after_loss?: ReentryAfterLoss
  quantity_by_symbol?: QuantityBySymbol[]
}

/** Winning / losing / flat days on the net basis (after estimated charges), by close date. */
export interface NetDayStats {
  basis?: string
  total_days: number
  winning_days: number
  losing_days: number
  flat_days: number
  /** Percent, 0-100, of `total_days`. */
  winning_day_pct: number
  /** Days left out because their charges are incomplete. */
  net_unavailable_days: number
}

export interface TradesPerDay {
  basis?: string
  days: number
  avg: number
  max: number
  max_day: string
}

/** Next entry after each losing position; objective timing only. */
export interface ReentryAfterLoss {
  window_minutes: number
  losses_followed_by_entry: number
  reentries_within_window: number
  shortest_gap_minutes: number
}

export interface QuantityBySymbol {
  symbol: string
  exchange: string
  positions: number
  min_quantity: number
  median_quantity: number
  max_quantity: number
}

/** Timing of the deepest net drawdown; absent when there was no drawdown. */
export interface DrawdownDetail {
  depth: number
  start_at?: string
  start_basis: string
  trough_at: string
  recovered: boolean
  /** Absent when it never recovered. */
  recovered_at?: string
  duration_minutes?: number
  duration_basis?: string
}

/** "How you trade" breakdowns (entry time of day, entry weekday, holding time). */
export interface TradingBreakdowns {
  time_of_day: BreakdownRow[]
  weekday: BreakdownRow[]
  hold_bucket: BreakdownRow[]
}

/**
 * Candidate executions of the period in one state each:
 * collected = awaiting_attribution + attributed_open + included + not_placed.
 */
export interface DataQuality {
  scope?: string
  collected: number
  awaiting_attribution: number
  awaiting_grace: number
  awaiting_other: number
  grace_minutes?: number
  earliest_attributable_at?: string | null
  attributed_open: number
  included: number
  not_placed: number
  reconciles: boolean
  charges_incomplete_legs: number
  charges_incomplete_trades: number
  prices: {
    requested: boolean
    open_positions: number
    statuses: Record<string, number>
    missing: number
    stale: number
  }
  sync: {
    last_successful_sync: string | null
    days_without_sync: string[]
    problems: string[]
  }
}

export interface ProvisionalFill {
  fill_ref: number
  role: 'ENTRY' | 'EXIT'
  side: string
  qty_used: number
  price: number
  fill_time: string | null
  charge_allocated: number | null
}

/** A completed trade rebuilt from UNRESOLVED fills: not in any total. */
export interface ProvisionalTrade {
  id: number
  provisional: true
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
  legs: number
  opening_unknown: boolean
  fills: ProvisionalFill[]
  /** True when an open position of the same instrument exists; see overlap_note. */
  overlaps_open_position?: boolean
  overlap_note?: string | null
}

export interface ProvisionalOpenPosition {
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  direction: 'LONG' | 'SHORT'
  open_qty: number
  avg_entry: number
  opened_at: string
  realized_so_far: number | null
  opening_unknown: boolean
  /** Provisional positions carry a negative id (never a ledger id). */
  id?: number
  overlaps_open_position?: boolean
  overlap_note?: string | null
}

export interface ProvisionalBlock {
  definition?: string
  fills: number
  trades: ProvisionalTrade[]
  open_positions: ProvisionalOpenPosition[]
  orphans: UnresolvedFill[]
  metrics: {
    trades: number
    wins: number
    losses: number
    win_rate: number | null
    profit_factor: number | null
    expectancy: number | null
    avg_win: number | null
    avg_loss: number | null
    stats_unavailable_reason?: string | null
    realized_gross: number
    charges: number
    net_of_charges: number
    charges_unavailable_trades: number
    net_charges_complete: boolean
    charges_missing_completed?: number
    low_sample?: boolean
  }
  sample: SampleContext
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
  /** Number of closing fills (partial exits included) in this trade. */
  legs: number
  position_seq?: number
  category?: 'intraday' | 'swing' | 'unclassified'
  /** Why the position is unclassified (a reason code); null otherwise. */
  category_reason?: string | null
  /** Exit minus entry for a long, entry minus exit for a short, per unit. */
  points_per_unit?: number | null
  opening_unknown?: boolean
  has_orphan_fills?: boolean
  orphan_fills?: number
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

/** A position whose FINAL close is in the current session (one row per position, flat to flat). */
export interface TodayClosedPosition {
  /** Position id within `build_id` (the same id the fills drill-down takes). */
  id: number
  build_id?: string | null
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  direction: 'LONG' | 'SHORT'
  /** Full position size over its life (every closing leg summed). */
  total_quantity: number
  /** Quantity-weighted average entry / exit. */
  entry_price: number
  exit_price: number
  opened_at: string
  /** Session date of the entry; earlier than the session for a carried position. */
  opened_session: string | null
  closed_at: string
  /** Gross P&L of the whole position (all its legs, whatever day). */
  gross_pnl: number
  /** Gross P&L of the legs closed in this session (part of today's realized). */
  realized_today: number
  charges: number | null
  charges_status: ChargesStatus
  net_pnl: number | null
  legs: number
  status: 'Closed'
}

/** A position open now (whatever day it opened); "Partially closed" when it has exit legs. */
export interface TodayOpenPosition {
  id?: number | null
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  direction: 'LONG' | 'SHORT'
  open_qty: number
  closed_qty: number
  total_quantity: number
  avg_entry: number
  opened_at: string
  opened_session?: string | null
  realized_so_far?: number | null
  realized_today: number
  exit_legs: number
  ltp?: number | null
  price_status: PriceStatus
  unrealized: number | null
  status: 'Open' | 'Partially closed'
}

/** An unresolved fill held by the manual grace window; never counted until attributed. */
export interface GraceHeldFill {
  id: number
  broker: string
  account: string
  symbol: string
  exchange: string
  product: string
  side: string
  qty: number
  price: number
  fill_time: string
  /** fill_time + grace: the earliest time a sync can classify it. */
  attributable_from: string
  /** The window has passed: it waits only for the next ledger sync. */
  due: boolean
  /** The fill's identity has an open discretionary position (its quantity may change). */
  affects_open_position: boolean
}

export interface PendingAttribution {
  grace_minutes: number
  last_evaluated_at: string | null
  grace_count: number
  earliest_attributable_at: string | null
  grace_fills: GraceHeldFill[]
  /** Unresolved fills of the session held for another reason (coverage, tag mode, ...). */
  other_today: number
  other_total: number
}

/**
 * The current trading session (session date of now, IST), from the same build as the rest of the
 * summary. Ignores the page period; honours the broker / account / symbol filters.
 */
export interface DiscretionaryToday {
  session_date: string
  session_definition?: string
  realized_gross: number
  charges: number
  charges_unavailable_legs: number
  charges_complete: boolean
  net: number
  realized_net_of_charges: number
  exit_legs: number
  /** Null when prices were not requested or none is usable. */
  unrealized: number | null
  unrealized_unavailable_positions: number
  price_statuses: Record<string, number>
  quotes_requested: boolean
  completed_positions: number
  open_positions: number
  partially_closed_positions: number
  metrics: Partial<
    Pick<
      DiscretionaryMetrics,
      | 'trades_count'
      | 'winning_trades'
      | 'losing_trades'
      | 'win_rate'
      | 'gross_profit'
      | 'gross_loss'
      | 'profit_factor'
      | 'expectancy'
      | 'avg_win'
      | 'avg_loss'
      | 'count_unavailable_reason'
    >
  >
  closed_positions: TodayClosedPosition[]
  open_position_rows: TodayOpenPosition[]
  pending_attribution: PendingAttribution
}

/** Counts of positions per category; `unclassified_reasons` maps a reason code to a count. */
export interface CategoryCounts {
  intraday: number
  swing: number
  unclassified: number
  unclassified_reasons: Record<string, number>
}

export interface ClassificationBlock {
  selected: DiscretionaryCategory
  closed: CategoryCounts
  open: CategoryCounts
  note: string
}

/** Unresolved and orphan fills whose session date is in the selected range (never in any figure). */
export interface UnresolvedInRange {
  unresolved_fills: number
  orphan_fills: number
  total: number
  start: string
  end: string
}

export interface ReconciliationBlock {
  basis?: string
  headline_net: number
  closed_positions_net: number
  difference: number
  reconciled: boolean
  [key: string]: unknown
}

/** Points are per unit (not per lot) and are never summed across instruments. */
export interface PointsByInstrumentRow {
  symbol: string
  exchange: string
  positions: number
  winners: number
  losers: number
  gross_points_gained: number
  gross_points_lost: number
  net_points: number
  avg_points_per_position: number | null
  avg_win_points: number | null
  avg_loss_points: number | null
  largest_win_points: number | null
  largest_loss_points: number | null
}

export interface StreakNow {
  type: 'win' | 'loss' | null
  length: number
}

/** Net realised drawdown of the closed positions, in rupees (percent fields are always null). */
export interface RiskBlock {
  basis?: string
  positions: number
  net_unavailable_positions: number
  max_drawdown: number
  current_drawdown: number
  peak_equity: number
  latest_equity: number
  max_drawdown_at: string | null
  max_drawdown_pct: number | null
  current_drawdown_pct: number | null
  percent_unavailable_reason?: string | null
  max_consecutive_wins: number
  max_consecutive_losses: number
  current_streak: StreakNow
  /** Absent when there was no drawdown or the server is older. */
  max_drawdown_detail?: DrawdownDetail
}

/** One close month or close weekday of the closed positions. */
export interface CalendarRow {
  /** Monthly rows: YYYY-MM. */
  period?: string
  /** Weekday rows: Mon .. Sun. */
  weekday?: string
  key: string
  positions: number
  wins: number
  losses: number
  /** Percent, 0-100; null when no position has a net. */
  win_rate: number | null
  realized_gross: number
  charges: number
  net_of_charges: number
  charges_unavailable_positions: number
  net_unavailable_positions: number
}

export interface DurationStat {
  count: number
  mean_minutes: number | null
  median_minutes: number | null
}

export interface TradeManagementBlock {
  net_basis_positions: number
  net_unavailable_positions: number
  break_even_excluded: number
  median_net_pnl: number | null
  avg_net_pnl: number | null
  winners: DurationStat
  losers: DurationStat
}

export interface CostsBlock {
  total_charges: number
  gross_profit: number
  charges_pct_of_gross_profit: number | null
  reason: string | null
  charges_unavailable_positions: number
}

export interface DiscretionarySummary {
  /** The current session; absent from older servers. */
  today?: DiscretionaryToday | null
  /** The category the figures are for; absent from older servers. */
  category?: DiscretionaryCategory
  /** Null when the server could not compute it (a warning says so); absent from older servers. */
  classification?: ClassificationBlock | null
  unresolved_in_range?: UnresolvedInRange | null
  reconciliation?: ReconciliationBlock | null
  points_by_instrument?: PointsByInstrumentRow[] | null
  risk?: RiskBlock | null
  monthly?: CalendarRow[] | null
  weekday?: CalendarRow[] | null
  trade_management?: TradeManagementBlock | null
  costs?: CostsBlock | null
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
  /**
   * Ledger fills of the period and filters that are manual, ui_manual or unresolved (strategy
   * fills are not counted). Read-only; absent from older servers.
   */
  executions_collected?: number
  /** The unresolved part of executions_collected: awaiting attribution, excluded from P&L. */
  executions_unresolved?: number
  /** Ledger fills of the period per read-time source label (SIGNAL_LIVE / MANUAL / UNCLASSIFIED). */
  source_labels?: Record<string, number>
  /** Sample-size context of the ratios; absent from older servers. */
  sample?: SampleContext
  /** Four-state count of the period's candidate executions; absent from older servers. */
  data_quality?: DataQuality | null
  /** Unresolved fills replayed as if manual; never in any total. Absent from older servers. */
  provisional?: ProvisionalBlock | null
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
  /** "How you trade" rows; absent from older servers. */
  breakdowns?: TradingBreakdowns | null
  /** Risk and consistency strip; absent from older servers. */
  consistency?: ConsistencyBlock | null
  trades: DiscretionaryTrade[]
  trades_total: number
  /** Rows in `trades` (the table is cut at a limit; every total covers all trades). */
  trades_shown?: number
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
  /** Read-time source label: SIGNAL_LIVE, MANUAL or UNCLASSIFIED (display only). */
  source_label?: string
  /** Short label of the broker order's source evidence; null when none was recorded. */
  source_evidence?: string | null
}

export interface UnresolvedResponse {
  unresolved: UnresolvedFill[]
  unresolved_count: number
  orphans: UnresolvedFill[]
  orphans_count: number
}

export interface ExcursionPosition {
  id: number
  symbol: string
  exchange: string
  direction: 'LONG' | 'SHORT'
  category: 'intraday' | 'swing' | 'unclassified'
  opened_at: string
  closed_at: string
  closed_quantity: number
  points_per_unit: number | null
  net_pnl: number | null
  status: 'available' | 'unavailable'
  reason: string | null
  reason_text: string | null
  mae_points: number | null
  mfe_points: number | null
  mae_amount: number | null
  mfe_amount: number | null
  minute_bar_precision: boolean
  profit_capture: number | null
}

export interface ExcursionDist {
  count: number
  average: number | null
  median: number | null
}

export interface ExcursionGroup {
  mae_points: ExcursionDist
  mfe_points: ExcursionDist
  mae_amount: ExcursionDist
  mfe_amount: ExcursionDist
}

export interface ExcursionAggregates {
  winners: ExcursionGroup & { profit_capture: ExcursionDist }
  losers: ExcursionGroup
  coverage: {
    available: number
    total: number
    label: string
    unavailable_reasons: Record<string, number>
  }
  minute_bar_precision: boolean
  precision_note: string
}

/** MAE / MFE of the closed positions of a scope (lazy, separate from the summary). */
export interface ExcursionsResponse {
  build_id: string | null
  period: { period: string; start: string; end: string }
  category: DiscretionaryCategory
  positions: ExcursionPosition[]
  positions_total: number
  computed: number
  cap: number
  aggregates: ExcursionAggregates
  note: string
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

/**
 * The query shared by the summary, the excursions and the CSV export, so they cannot drift:
 * period (and the custom range), the broker / account / symbol filters and the category.
 */
export function scopeParams(
  period: PerformancePeriod,
  range: DiscretionaryRange | undefined,
  filters: DiscretionaryFilters,
  category: DiscretionaryCategory = 'all'
): URLSearchParams {
  const params = buildParams(filters, { period, category })
  if (period === 'custom' && range) {
    params.set('start', range.start)
    params.set('end', range.end)
  }
  return params
}

export function summaryParams(
  period: PerformancePeriod,
  range: DiscretionaryRange | undefined,
  filters: DiscretionaryFilters,
  quotes: boolean,
  category: DiscretionaryCategory = 'all'
): URLSearchParams {
  const params = scopeParams(period, range, filters, category)
  params.set('quotes', quotes ? '1' : '0')
  return params
}

/** Link of the CSV download: the summary's scope (same params function), no quotes. */
export function exportCsvUrl(
  period: PerformancePeriod,
  range: DiscretionaryRange | undefined,
  filters: DiscretionaryFilters,
  category: DiscretionaryCategory = 'all'
): string {
  return `/api/discretionary-pnl/export.csv?${scopeParams(period, range, filters, category).toString()}`
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
    quotes: boolean,
    category: DiscretionaryCategory = 'all'
  ): Promise<DiscretionarySummary> =>
    read(
      webClient.get<DiscretionarySummary & { status?: string }>(
        `/api/discretionary-pnl/summary?${summaryParams(period, range, filters, quotes, category).toString()}`
      ),
      'Could not load the discretionary P&L'
    ),

  /** MAE / MFE of the closed positions of the same scope; slow, so it is a separate call. */
  excursions: (
    period: PerformancePeriod,
    range: DiscretionaryRange | undefined,
    filters: DiscretionaryFilters,
    category: DiscretionaryCategory = 'all'
  ): Promise<ExcursionsResponse> =>
    read(
      webClient.get<ExcursionsResponse & { status?: string }>(
        `/api/discretionary-pnl/excursions?${scopeParams(period, range, filters, category).toString()}`
      ),
      'Could not load MAE / MFE'
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
