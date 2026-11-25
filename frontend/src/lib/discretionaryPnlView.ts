import type {
  BreakdownRow,
  CalendarRow,
  ClassificationBlock,
  ConsistencyBlock,
  DiscretionaryCategory,
  DiscretionaryMetrics,
  DiscretionaryPosition,
  DiscretionarySummary,
  DiscretionaryToday,
  DiscretionaryTrade,
  ExcursionPosition,
  ExcursionsResponse,
  PendingAttribution,
  PeriodRow,
  PriceStatus,
  ProvisionalBlock,
  ProvisionalOpenPosition,
  RiskBlock,
  SampleContext,
} from '@/api/discretionaryPnl'

/** The Performance page source: current-mode strategies (unchanged) or discretionary trading. */
export type PageSource = 'strategies' | 'discretionary'

export type SourceAvailability = 'pending' | 'enabled' | 'disabled'

/** URL value that selects the Discretionary source. Absent or anything else means strategies. */
export const DISCRETIONARY_SOURCE = 'discretionary'

/** Mirrors the Performance page's poll interval; quotes=1 polls at the same pace. */
export const DISCRETIONARY_POLL_MS = 60_000

export const NET_EQUITY_CAPTION = 'Net P&L after estimated charges, by close date'
/** Moved-out detail of the chart captions: shown as a tooltip on the caption and axis note. */
export const EQUITY_CAPTION_DETAIL =
  'Net realised P&L after estimated charges, end-of-day value by close date. Unrealized P&L is not included. This is not an account balance or a return on capital. The drawdown strip is end-of-day too, so it can differ from the per-trade drawdown in Risk and consistency. A weekday with no dot had no recorded trades: it is a gap, not a day of zero. Hover a point for its time and P&L.'
export const DRAWDOWN_BASIS = 'gross realized, excludes charges and unrealized'
export const NET_DRAWDOWN_BASIS = 'net realised, after estimated charges, by close time'

// Small-text colours. Light theme needs the 700 shades for AA on white; dark theme keeps the
// existing green and uses a lighter red. `.analyzer` is a dark palette that does not carry the
// `.dark` class (the theme store swaps one for the other), so it gets its own partner for green;
// its red is already remapped in index.css.
export const POSITIVE_TEXT = 'text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600'
export const NEGATIVE_TEXT = 'text-red-600 dark:text-red-400'

/** The source asked for by the URL, ignoring whether the backend allows it. */
export function parseSourceParam(raw: string | null | undefined): PageSource {
  return raw === DISCRETIONARY_SOURCE ? 'discretionary' : 'strategies'
}

/**
 * The source the page should show. 'discretionary' needs the backend to say it is enabled; while
 * that is still unknown the page waits ('pending') rather than starting strategy requests; a
 * disabled or failed status falls back to strategies.
 */
export function resolveSource(
  raw: string | null | undefined,
  availability: SourceAvailability
): PageSource | 'pending' {
  if (parseSourceParam(raw) !== 'discretionary') return 'strategies'
  if (availability === 'enabled') return 'discretionary'
  if (availability === 'pending') return 'pending'
  return 'strategies'
}

/** Price badge for an open position. */
export interface PriceBadge {
  label: string
  tone: 'ok' | 'warn' | 'bad' | 'muted'
  /** Plain-words explanation, for a tooltip. */
  title: string
}

/** "12s", "3m" or "2h 05m" for a quote age in seconds; empty when unknown. */
export function fmtAge(ageSeconds: number | null | undefined): string {
  if (ageSeconds == null || Number.isNaN(ageSeconds) || ageSeconds < 0) return ''
  const s = Math.round(ageSeconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

export function priceBadge(
  status: PriceStatus | string | null | undefined,
  ageSeconds?: number | null
): PriceBadge {
  const age = fmtAge(ageSeconds)
  switch (status) {
    case 'live':
      return { label: age ? `Live ${age}` : 'Live', tone: 'ok', title: 'Live quote' }
    case 'stale':
      return {
        label: age ? `Stale ${age}` : 'Stale',
        tone: 'warn',
        title: 'The quote is older than expected; the price may have moved.',
      }
    case 'last_close':
      return {
        label: 'Last close',
        tone: 'warn',
        title: 'The market is closed: priced at the last close.',
      }
    case 'cached':
      return {
        label: age ? `Cached ${age}` : 'Cached',
        tone: 'warn',
        title: 'The quote call failed: priced from an earlier quote.',
      }
    case 'unavailable':
      return { label: 'No price', tone: 'bad', title: 'No usable price; unrealized is unknown.' }
    case 'not_requested':
      return {
        label: 'Not requested',
        tone: 'muted',
        title: 'Prices are not fetched unless you turn on Fetch live prices.',
      }
    default:
      return { label: 'Unknown', tone: 'muted', title: 'Unknown price status' }
  }
}

export interface ChargesBadge {
  label: string
  tone: 'muted' | 'warn'
  title: string
}

/** Badge for the charges of a closed trade: estimated, or unavailable when the cost model has none. */
export function chargesBadge(trade: Pick<DiscretionaryTrade, 'charges_status'>): ChargesBadge {
  if (trade.charges_status === 'ok') {
    return {
      label: 'estimated',
      tone: 'muted',
      title: 'Estimated from the fee schedule, not a broker statement.',
    }
  }
  return {
    label: 'charges unavailable',
    tone: 'warn',
    title: 'The instrument is not modelled: net P&L counts its charges as 0.',
  }
}

/** Win rate arrives as a percent (0-100). */
export function fmtWinRate(percent: number | null | undefined): string {
  if (percent == null || Number.isNaN(percent)) return '—'
  return `${percent.toFixed(1)}%`
}

/**
 * A net-basis money figure (average win / loss, expectancy): a dash when null, i.e. when no
 * position has a net P&L. Never a 0 that would read as a real result.
 */
export function fmtNetMoney(
  value: number | null | undefined,
  formatCurrency: (v: number) => string
): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return formatCurrency(value)
}

/** Profit factor: null means profit with no loss when there are wins, otherwise nothing to show. */
export function fmtProfitFactor(
  value: number | null | undefined,
  wins: number,
  losses: number,
  /** False when no position has a net P&L: the figure is unknown, not "not applicable". */
  hasNet = true
): string {
  if (!hasNet) return '—'
  // No profit and no loss (every trade break-even): the server may send 0, which is not a result.
  if (wins === 0 && losses === 0) return NOT_APPLICABLE
  if (value != null && Number.isFinite(value)) return value.toFixed(2)
  if (wins > 0 && losses === 0) return '∞'
  return '—'
}

/** Shown where a figure cannot exist (for example the average loss with no losing trade). */
export const NOT_APPLICABLE = 'n/a'

/**
 * Average win or loss: 'n/a' when there is no trade of that kind (decided from the count, so a
 * server-side 0 never reads as Rs 0), else the net-basis figure.
 */
export function fmtAvgByCount(
  value: number | null | undefined,
  count: number,
  formatCurrency: (v: number) => string
): string {
  if (count <= 0) return NOT_APPLICABLE
  return fmtNetMoney(value, formatCurrency)
}

/** Average win divided by average loss; 'n/a' unless there is at least one win and one loss. */
export function fmtPayoffRatio(
  avgWin: number | null | undefined,
  avgLoss: number | null | undefined,
  wins: number,
  losses: number
): string {
  if (wins <= 0 || losses <= 0) return NOT_APPLICABLE
  if (avgWin == null || avgLoss == null || !Number.isFinite(avgWin) || !Number.isFinite(avgLoss))
    return NOT_APPLICABLE
  if (Math.abs(avgLoss) === 0) return NOT_APPLICABLE
  return (Math.abs(avgWin) / Math.abs(avgLoss)).toFixed(2)
}

export interface WinRateParts {
  wins: number
  losses: number
  /** Null when the server did not send the net-basis count. */
  breakEven: number | null
  /** Completed trades with a net P&L: the denominator of the win rate. Null when not sent. */
  sample: number | null
}

/** Wins, losses, break-even and sample size behind the win rate. Break-even is derived. */
export function winRateParts(
  m: Pick<DiscretionaryMetrics, 'winning_trades' | 'losing_trades' | 'net_basis_trades'>
): WinRateParts {
  const sample = m.net_basis_trades ?? null
  const breakEven = sample == null ? null : Math.max(0, sample - m.winning_trades - m.losing_trades)
  return {
    wins: m.winning_trades,
    losses: m.losing_trades,
    breakEven,
    sample,
  }
}

/** "N of M completed positions have 1-minute price bars" from the excursion coverage counts. */
export function coverageText(withData: number, total: number): string {
  return `${withData} of ${total} completed positions have 1-minute price bars`
}

/**
 * Reasons positions lack MAE / MFE, as full phrases: "6 positions: price data for that day is
 * not complete yet". `texts` maps a reason code to the server's trader wording.
 */
export function excursionReasonPhrases(
  counts: Record<string, number> | null | undefined,
  texts: ReadonlyMap<string, string>
): string[] {
  return Object.entries(counts ?? {}).map(([code, n]) => {
    const text = texts.get(code) ?? code.replace(/_/g, ' ')
    return `${n} ${n === 1 ? 'position' : 'positions'}: ${text}`
  })
}

/** The one line for a range in which no trade was closed. */
export const NO_CLOSED_TRADES_TEXT = 'No closed trades in this date range.'

/**
 * True when nothing was closed in the range: no completed trade and no leg closed (a partial
 * exit still has realized money). Realized money, charges and every ratio are then unknown for
 * the range, so the page shows a dash instead of 0.00.
 */
export function hasNoClosedActivity(
  summary:
    | (Pick<DiscretionarySummary, 'metrics'> & Partial<Pick<DiscretionarySummary, 'by_period'>>)
    | undefined
): boolean {
  if (!summary) return true
  return (summary.metrics?.trades_count ?? 0) === 0 && (summary.by_period?.daily?.length ?? 0) === 0
}

/**
 * True when the whole report range ends before the first reliable date, so nothing in it can
 * be rebuilt. Custom ranges use the requested end; other periods use the summary's own end.
 */
export function rangeEndsBeforeReliableStart(
  period: string,
  range: { start: string; end: string } | undefined,
  summary: Pick<DiscretionarySummary, 'period'> | undefined
): boolean {
  const since = summary?.period.reliable_since
  const end = period === 'custom' ? range?.end : summary?.period.end
  return !!since && !!end && end < since
}

/**
 * True when an empty page means the ledger itself has no trades: the report range runs from the
 * first reliable date (the first synced fill, or today when there is none) up to today. A range
 * that stops short of either end can be empty while the ledger holds trades elsewhere.
 */
export function ledgerIsEmpty(
  summary: Pick<DiscretionarySummary, 'period'>,
  today: string
): boolean {
  const since = summary.period.reliable_since
  return !since || (summary.period.start <= since && summary.period.end >= today)
}

export const INSUFFICIENT_DATA = 'insufficient data'

/** Minutes as "45 min", "2 h 5 min" or "1 d 3 h" (calendar time between two timestamps). */
export function fmtSpan(minutes: number | null | undefined): string {
  if (minutes == null || !Number.isFinite(minutes)) return '—'
  const m = Math.round(minutes)
  if (m < 60) return `${m} min`
  if (m < 1440) return `${Math.floor(m / 60)} h ${m % 60} min`
  return `${Math.floor(m / 1440)} d ${Math.floor((m % 1440) / 60)} h`
}

/** Winning-day share on the net basis with its sample (days); null when the server did not send it. */
export function winningDaysText(
  c: Pick<ConsistencyBlock, 'net_days'> | null | undefined
): { value: string; hint: string } | null {
  const d = c?.net_days
  if (!d) return null
  const extra = d.flat_days > 0 ? `, ${d.flat_days} flat` : ''
  return {
    value: `${d.winning_day_pct.toFixed(1)}%`,
    hint: `${d.winning_days} of ${d.total_days} day${d.total_days === 1 ? '' : 's'} net positive${extra}`,
  }
}

/** Deepest net drawdown: when it began, whether it recovered, and for how long. */
export function drawdownRecoveryText(
  risk: Pick<RiskBlock, 'max_drawdown' | 'max_drawdown_detail'> | null | undefined
): { value: string; hint: string } {
  if (!risk) return { value: INSUFFICIENT_DATA, hint: '' }
  const d = risk.max_drawdown_detail
  if (!d) {
    return risk.max_drawdown > 0
      ? { value: INSUFFICIENT_DATA, hint: 'timing not sent by the server' }
      : { value: 'No drawdown', hint: 'net P&L never fell below an earlier high' }
  }
  const began = d.start_at ? `; began ${fmtStamp(d.start_at)}` : ''
  if (d.recovered) {
    return {
      value:
        d.duration_minutes != null ? `Recovered after ${fmtSpan(d.duration_minutes)}` : 'Recovered',
      hint: `fell to ${fmtStamp(d.trough_at)}, back at its high ${fmtStamp(d.recovered_at)}${began}`,
    }
  }
  return {
    value:
      d.duration_minutes != null
        ? `Not recovered, ${fmtSpan(d.duration_minutes)}`
        : 'Not recovered',
    hint: `fell to ${fmtStamp(d.trough_at)}; still below its high at the latest close${began}`,
  }
}

/** Trades per day: average and busiest day. */
export function tradesPerDayText(c: Pick<ConsistencyBlock, 'trades_per_day'> | null | undefined): {
  value: string
  hint: string
} {
  const t = c?.trades_per_day
  if (!t) return { value: INSUFFICIENT_DATA, hint: '' }
  return {
    value: `${t.avg} avg, ${t.max} max`,
    hint: `busiest day ${t.max_day}; ${t.days} trading day${t.days === 1 ? '' : 's'}`,
  }
}

/** Objective re-entry timing after a losing trade. No judgement about the reason. */
export function reentryText(c: Pick<ConsistencyBlock, 'reentry_after_loss'> | null | undefined): {
  value: string
  hint: string
} {
  const r = c?.reentry_after_loss
  if (!r)
    return { value: INSUFFICIENT_DATA, hint: 'needs a losing trade followed by another entry' }
  return {
    value: `${r.reentries_within_window} of ${r.losses_followed_by_entry}`,
    hint: `Re-entered within ${r.window_minutes} min after a loss; shortest gap ${fmtSpan(r.shortest_gap_minutes)}`,
  }
}

/**
 * Position size (quantity) spread per symbol: smallest, median, largest. `key` is unique per
 * exchange and symbol; `label` carries the exchange only when more than one exchange traded the
 * symbol.
 */
export function quantityText(
  c: Pick<ConsistencyBlock, 'quantity_by_symbol'> | null | undefined
): { key: string; symbol: string; label: string; text: string; positions: number }[] {
  const rows = c?.quantity_by_symbol ?? []
  const exchanges = new Map<string, Set<string>>()
  for (const q of rows) {
    const set = exchanges.get(q.symbol) ?? new Set<string>()
    set.add(q.exchange)
    exchanges.set(q.symbol, set)
  }
  return rows.map((q) => ({
    key: `${q.exchange}:${q.symbol}`,
    symbol: q.symbol,
    label:
      (exchanges.get(q.symbol)?.size ?? 0) > 1 && q.exchange
        ? `${q.symbol} (${q.exchange})`
        : q.symbol,
    positions: q.positions,
    text: `${fmtQty(q.min_quantity)} / ${fmtQty(q.median_quantity)} / ${fmtQty(q.max_quantity)}`,
  }))
}

/** Quantity as an integer when whole, otherwise trimmed. */
export function fmtQty(qty: number | null | undefined): string {
  if (qty == null || Number.isNaN(qty)) return '—'
  return Number.isInteger(qty) ? String(qty) : String(Number(qty.toFixed(4)))
}

/** Price with two decimals; an em dash when unknown. */
export function fmtPrice(price: number | null | undefined): string {
  if (price == null || Number.isNaN(price)) return '—'
  return price.toFixed(2)
}

/** "2026-11-17 09:31" (backend IST naive timestamp) trimmed to the minute. */
export function fmtStamp(value: string | null | undefined): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 16)
}

export type UnrealizedKind = 'none' | 'not_requested' | 'partial' | 'complete'

export interface UnrealizedSummary {
  kind: UnrealizedKind
  /** Sum of the priced positions; meaningful for 'complete' and 'partial'. */
  value: number
  unpriced: number
  /** Worst price status among open positions, for the badge. */
  badge: PriceBadge | null
}

const BADGE_RANK: Record<string, number> = {
  unavailable: 5,
  cached: 4,
  stale: 3,
  last_close: 2,
  live: 1,
  not_requested: 0,
}

/** Whether the unrealized figure is known, and how good the prices behind it are. */
export function summarizeUnrealized(
  summary: Pick<
    DiscretionarySummary,
    'open_positions' | 'positions' | 'positions_truncated' | 'unrealized_total' | 'quotes_requested'
  >
): UnrealizedSummary {
  const positions = summary.positions ?? []
  if (summary.open_positions === 0 && positions.length === 0) {
    return { kind: 'none', value: 0, unpriced: 0, badge: null }
  }
  if (!summary.quotes_requested) {
    return {
      kind: 'not_requested',
      value: 0,
      unpriced: positions.length,
      badge: priceBadge('not_requested'),
    }
  }
  const unpriced = positions.filter((p) => p.unrealized == null).length
  const worst = worstPosition(positions)
  const partial = unpriced > 0 || (summary.positions_truncated ?? 0) > 0
  return {
    kind: partial ? 'partial' : 'complete',
    value: summary.unrealized_total ?? 0,
    unpriced,
    badge: worst ? priceBadge(worst.price_status, worst.age_s) : null,
  }
}

function worstPosition(positions: DiscretionaryPosition[]): DiscretionaryPosition | null {
  let worst: DiscretionaryPosition | null = null
  for (const p of positions) {
    if (!worst || (BADGE_RANK[p.price_status] ?? 0) > (BADGE_RANK[worst.price_status] ?? 0)) {
      worst = p
    }
  }
  return worst
}

/** Net realized plus unrealized when the unrealized figure is complete; otherwise net realized alone. */
export function totalPnl(
  netRealized: number,
  unrealized: UnrealizedSummary
): { value: number; includesUnrealized: boolean } {
  if (unrealized.kind === 'complete') {
    return { value: netRealized + unrealized.value, includesUnrealized: true }
  }
  if (unrealized.kind === 'none') return { value: netRealized, includesUnrealized: true }
  return { value: netRealized, includesUnrealized: false }
}

export interface TotalCard {
  label: string
  value: number
  hint: string
  /** True when either part of the total is incomplete. */
  incomplete: boolean
}

/**
 * The Total card. Never says "unrealized not included" next to a non-zero Unrealized card: a
 * partly priced book shows the partial sum, flagged with how many positions are unpriced.
 */
export function totalCard(
  netRealized: number,
  unrealized: UnrealizedSummary,
  chargesUnavailableTrades: number
): TotalCard {
  const netNote =
    chargesUnavailableTrades > 0
      ? `net incomplete: ${chargesUnavailableTrades} trade(s) without a charge estimate`
      : ''
  const join = (first: string) => (netNote ? `${first}; ${netNote}` : first)
  if (unrealized.kind === 'partial') {
    const why =
      unrealized.unpriced > 0
        ? `${unrealized.unpriced} positions unpriced`
        : 'some positions not shown'
    return {
      label: `Net realized + unrealized (partial: ${why})`,
      value: netRealized + unrealized.value,
      hint: join('partial total'),
      incomplete: true,
    }
  }
  if (unrealized.kind === 'not_requested') {
    return {
      label: 'Total',
      value: netRealized,
      hint: join('net realized only: live prices not requested'),
      incomplete: true,
    }
  }
  return {
    label: 'Total',
    value: netRealized + unrealized.value,
    hint: join('net realized + unrealized'),
    incomplete: netNote !== '',
  }
}

/** Text colour for a P&L figure: darker green in light theme (AA), lighter red in dark theme. */
export function pnlTextClass(value: number): string {
  return value > 0 ? POSITIVE_TEXT : value < 0 ? NEGATIVE_TEXT : ''
}

/** Plain words for the reason codes the backend attaches to fills it could not use. */
const REASON_TEXT: Record<string, string> = {
  invalid_fill: 'The fill had missing or invalid data',
  opening_unknown: 'The fill that opened this position is not in the ledger',
  excess_over_position_opening_unknown:
    'Sold or bought more than the known position; the opening fill is not in the ledger',
  ambiguous_attribution:
    'Could not tell whether a strategy or you placed this order, so it is left out of P&L',
  unresolved: 'Not yet attributed to a strategy or to you, so it is left out of P&L',
  opening_not_in_ledger: 'The fill that opened this position is not in the ledger',
  // why an unresolved fill is still unresolved (server `reason` of the unresolved list)
  before_coverage: 'Fill dated before attribution coverage began',
  within_grace: 'Inside the safety window after the fill: a strategy tag may still arrive',
  evidence_conflict: 'Conflicting evidence: the order tag and the order source disagree',
  system_order: 'Broker, RMS or system-generated order (for example a square-off): never manual',
  api_untagged: 'API order without an OpenAlgo tag: automated, strategy unknown',
  non_human_source: 'The order source is not a recognised manual channel',
  no_order_id: 'No usable broker order id',
  non_live_tag: 'OpenAlgo tag of a non-live (sandbox) order',
  unrecognised_remarks: 'Unrecognised OpenAlgo order remarks',
  unknown_mode_tag: 'Strategy tag of an unknown trade mode',
  engine_unreadable: 'The strategy engine trade records could not be read',
  engine_unknown_mode: 'Strategy engine order of an unknown trade mode',
  awaiting_sync: 'Can now be attributed: updates at the next ledger sync',
  unknown: 'Reason not determined',
}

/**
 * Plain text for a raw reason or attribution code; unknown codes are shown with spaces.
 * `coverageFrom` (ISO date) is added to the before-coverage text when the server sent it.
 */
export function reasonText(code: string | null | undefined, coverageFrom?: string | null): string {
  if (!code) return '—'
  const text = REASON_TEXT[code] ?? code.replace(/_/g, ' ')
  if (code === 'before_coverage' && coverageFrom) return `${text} (${fmtDayMonth(coverageFrom)})`
  return text
}

/** Plain-text net definition (the server sends field names; the page shows what they mean). */
export function netDefinitionText(raw: string | undefined): string {
  if (!raw) return ''
  return 'Net = gross realized minus estimated charges.'
}

/** Tooltip of the net definition line. */
export const NET_DEFINITION_DETAIL =
  'Gross realized is before charges. Win rate, profit factor, expectancy and the averages are measured on net P&L, after estimated charges.'

/** True when a net figure leaves out charges it could not estimate. */
export function netIncomplete(row: {
  net_charges_complete?: boolean
  charges_unavailable_trades?: number
}): boolean {
  return row.net_charges_complete === false || (row.charges_unavailable_trades ?? 0) > 0
}

/**
 * True when there is nothing to show at all: no completed trades, no open positions and no
 * closing leg in the period (a partial exit of a position completed after the period still
 * has realized money to show).
 */
export function isEmptyBook(
  summary:
    | (Pick<DiscretionarySummary, 'trades_total' | 'positions_total'> &
        Partial<Pick<DiscretionarySummary, 'by_period'>>)
    | undefined
): boolean {
  if (!summary) return true
  return (
    (summary.trades_total ?? 0) === 0 &&
    (summary.positions_total ?? 0) === 0 &&
    (summary.by_period?.daily?.length ?? 0) === 0
  )
}

/**
 * Executions of the selected period and filters while the book is empty (no completed trade, no
 * open position, no closed leg) and some of them await attribution; null otherwise.
 * ``collected`` counts the discretionary candidates (manual + unresolved fills, never strategy
 * fills), ``unresolved`` the part awaiting attribution. An older server without the counts
 * gives null, so the page keeps its generic empty state.
 */
export function awaitingAttribution(
  summary:
    | (Pick<DiscretionarySummary, 'trades_total' | 'positions_total'> &
        Partial<
          Pick<DiscretionarySummary, 'by_period' | 'executions_collected' | 'executions_unresolved'>
        >)
    | undefined
): { collected: number; unresolved: number } | null {
  if (!summary || !isEmptyBook(summary)) return null
  const unresolved = summary.executions_unresolved ?? 0
  if (unresolved <= 0) return null
  return { collected: Math.max(summary.executions_collected ?? 0, unresolved), unresolved }
}

/** The empty-state message while every execution of the selection awaits attribution. */
export function awaitingAttributionText(collected: number, unresolved: number): string {
  return `No trades to show yet: ${unresolved} of ${collected} fills are not counted.`
}

/** Sorted distinct non-empty values, for a filter's choices. */
export function distinctOptions(values: (string | null | undefined)[]): string[] {
  return [...new Set(values.filter((v): v is string => !!v && v.trim() !== ''))].sort()
}

/** Brokers and accounts seen in a summary, for the filter choices. */
export function filterChoices(summary: DiscretionarySummary | undefined): {
  brokers: string[]
  accounts: string[]
} {
  if (!summary) return { brokers: [], accounts: [] }
  const rows = [
    ...(summary.per_identity ?? []),
    ...(summary.trades ?? []),
    ...(summary.positions ?? []),
  ]
  return {
    brokers: distinctOptions(rows.map((r) => r.broker)),
    accounts: distinctOptions(rows.map((r) => r.account)),
  }
}

/** Human label for a breakdown key such as LONG, MIS or intraday. */
export function breakdownLabel(key: string): string {
  const labels: Record<string, string> = {
    LONG: 'Long',
    SHORT: 'Short',
    intraday: 'Intraday',
    overnight: 'Overnight',
  }
  return labels[key] ?? key
}

/** Rows of a holding breakdown, in a fixed order, skipping absent buckets. */
export function holdingRows(
  holding: DiscretionarySummary['by_holding'] | undefined
): BreakdownRow[] {
  if (!holding) return []
  return (['intraday', 'overnight'] as const)
    .filter((name) => !!holding[name])
    .map((name) => ({ ...holding[name], key: name }))
}

// ---------------------------------------------------------------- today (current session)

/** Unrealized of today's open positions: the value when known and the worst price status. */
export function todayUnrealized(
  today: Pick<
    DiscretionaryToday,
    | 'open_positions'
    | 'quotes_requested'
    | 'unrealized'
    | 'unrealized_unavailable_positions'
    | 'price_statuses'
  >
): UnrealizedSummary {
  if (today.open_positions === 0) return { kind: 'none', value: 0, unpriced: 0, badge: null }
  if (!today.quotes_requested) {
    return {
      kind: 'not_requested',
      value: 0,
      unpriced: today.open_positions,
      badge: priceBadge('not_requested'),
    }
  }
  let worst: string | null = null
  for (const status of Object.keys(today.price_statuses ?? {})) {
    if (worst === null || (BADGE_RANK[status] ?? 0) > (BADGE_RANK[worst] ?? 0)) worst = status
  }
  const unpriced = today.unrealized_unavailable_positions ?? 0
  return {
    kind: today.unrealized == null ? 'none' : unpriced > 0 ? 'partial' : 'complete',
    value: today.unrealized ?? 0,
    unpriced,
    badge: worst ? priceBadge(worst) : null,
  }
}

/** "HH:MM" of a backend IST timestamp ("2026-11-17 11:20:00"). */
export function fmtClock(value: string | null | undefined): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(11, 16)
}

/**
 * Why some of today's executions are not counted yet; empty when nothing is pending. Fills held
 * by the grace window give their count and the earliest time a sync can classify them; fills
 * held for another reason point to the Unresolved fills list. Nothing here is counted in P&L.
 */
export function pendingAttributionLines(pending: PendingAttribution | undefined): string[] {
  if (!pending) return []
  const lines: string[] = []
  const held = pending.grace_count ?? 0
  if (held > 0) {
    const due = (pending.grace_fills ?? []).filter((f) => f.due).length
    const waiting = held - due
    let text = `${held} execution(s) await attribution (${pending.grace_minutes}-minute window) and are not in P&L yet.`
    if (waiting > 0 && pending.earliest_attributable_at) {
      const next = (pending.grace_fills ?? []).find((f) => !f.due)
      text += ` Earliest: ${fmtClock(next?.attributable_from ?? pending.earliest_attributable_at)} IST.`
    }
    if (due > 0) text += ` ${due} wait only for the next ledger sync.`
    if ((pending.grace_fills ?? []).some((f) => f.affects_open_position)) {
      text += ' An open position may still change.'
    }
    lines.push(text)
  }
  if ((pending.other_today ?? 0) > 0) {
    lines.push(`${pending.other_today} execution(s) today are unresolved and not in P&L.`)
  }
  return lines
}

/** True when the server sent provisional fills worth listing (trades, open quantities or orphans). */
export function hasProvisionalData(block: ProvisionalBlock | null | undefined): boolean {
  if (!block) return false
  return block.trades.length > 0 || block.open_positions.length > 0 || block.orphans.length > 0
}

/** Same instrument, same direction, same broker, account, exchange and product. */
function sameIdentity(a: DiscretionaryPosition, b: ProvisionalOpenPosition): boolean {
  return (
    a.broker === b.broker &&
    a.account === b.account &&
    a.exchange === b.exchange &&
    a.symbol === b.symbol &&
    a.product === b.product &&
    a.direction === b.direction
  )
}

/**
 * Note for a confirmed open-position row when provisional executions hold quantity in the same
 * instrument and direction. Shows both quantities side by side and never adds them: the
 * provisional quantity is not confirmed and is in no position or P&L figure. Null when nothing
 * matches (a different direction gives no note).
 */
export function openPositionProvisionalNote(
  position: DiscretionaryPosition,
  provisional: ProvisionalBlock | null | undefined
): string | null {
  if (!provisional) return null
  const matches = provisional.open_positions.filter((p) => sameIdentity(position, p))
  if (matches.length === 0) return null
  const qty = matches.reduce((sum, p) => sum + p.open_qty, 0)
  return `${fmtQty(position.open_qty)} confirmed qty + ${fmtQty(qty)} provisional qty awaiting attribution.`
}

/**
 * Executions awaiting the attribution window (the server's grace count) against the other
 * provisional fills, which are outside that window. Counts only; both come from the payload.
 */
export function provisionalCounts(
  provisional: ProvisionalBlock | null | undefined,
  pending: PendingAttribution | undefined
): { awaiting: number; historical: number } {
  const awaiting = pending?.grace_count ?? 0
  const historical = Math.max(0, (provisional?.fills ?? 0) - awaiting)
  return { awaiting, historical }
}

/** "2 execution(s) awaiting attribution; 5 historical fill(s) outside the 30-minute window." */
export function attributionCountsLine(awaiting: number, historical: number): string | null {
  const parts: string[] = []
  if (awaiting > 0) parts.push(`${awaiting} execution(s) awaiting attribution`)
  if (historical > 0) parts.push(`${historical} historical fill(s) outside the 30-minute window`)
  return parts.length > 0 ? `${parts.join('; ')}.` : null
}

// ---------------------------------------------------------------- dashboard additions

/** Backend timestamps are naive IST ("2026-11-17 09:46:18"); IST is UTC+05:30 all year. */
const IST_OFFSET_MS = 330 * 60_000

/** Epoch ms of a naive IST timestamp, whatever the browser's time zone; null when unparsable. */
export function parseIstStamp(value: string | null | undefined): number | null {
  if (!value) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(value)
  if (!m) return null
  const [, y, mo, d, h, mi, s] = m
  return Date.UTC(+y, +mo - 1, +d, +h, +mi, +(s ?? 0)) - IST_OFFSET_MS
}

/** Position age from its opening to `nowMs`: "45m", "2h 15m", "1d 3h"; '—' when unknown. */
export function fmtPositionAge(openedAt: string | null | undefined, nowMs: number): string {
  const opened = parseIstStamp(openedAt)
  if (opened == null) return '—'
  const minutes = Math.floor((nowMs - opened) / 60_000)
  if (minutes < 0) return '—'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m`
  return `${Math.floor(hours / 24)}d ${hours % 24}h`
}

export const DAILY_CHART_CAPTION = 'Daily net P&L after estimated charges.'

export interface DailyBar {
  date: string
  gross: number
  charges: number
  net: number
  netComplete: boolean
  /** Bar length as a percent of the largest absolute NET day (0-100). */
  pct: number
}

/** Bars of the daily P&L chart (one per day with a close, by NET P&L) and the day counts behind them. */
export function dailyBars(rows: PeriodRow[] | undefined): {
  bars: DailyBar[]
  profitable: number
  losing: number
  flat: number
} {
  const list = rows ?? []
  const peak = Math.max(0, ...list.map((r) => Math.abs(r.net_of_charges)))
  const bars = list.map((r) => ({
    date: r.period,
    gross: r.realized_gross,
    charges: r.charges,
    net: r.net_of_charges,
    netComplete: !netIncomplete(r),
    pct: peak > 0 ? (Math.abs(r.net_of_charges) / peak) * 100 : 0,
  }))
  return {
    bars,
    profitable: list.filter((r) => r.net_of_charges > 0).length,
    losing: list.filter((r) => r.net_of_charges < 0).length,
    flat: list.filter((r) => r.net_of_charges === 0).length,
  }
}

/**
 * "Ratios based on N completed trades over D trading days". Null when the range has no
 * completed trade: the sentence would only state a zero.
 */
export function sampleNote(sample: SampleContext | undefined): { text: string } | null {
  if (!sample || sample.completed_trades <= 0) return null
  const n = sample.completed_trades
  const d = sample.trading_days
  return {
    text: `Ratios based on ${n} completed trade${n === 1 ? '' : 's'} over ${d} trading day${d === 1 ? '' : 's'}.`,
  }
}

/**
 * Profit factor of a breakdown row: always the number (dash only when it is null). The row's
 * Trades column shows the sample behind it.
 */
export function rowProfitFactor(
  row: Pick<
    BreakdownRow,
    'trades' | 'profit_factor' | 'wins' | 'losses' | 'stats_unavailable_reason'
  >
): { value: string; title?: string } {
  if (row.trades === 0) return { value: '—', title: 'No completed trade in this group' }
  return {
    value: fmtProfitFactor(row.profit_factor, row.wins, row.losses, !row.stats_unavailable_reason),
  }
}

export type HowYouTradeKey = 'time_of_day' | 'weekday' | 'hold_bucket'

/**
 * Selector options of the "How you trade" card. Symbol, Long/Short, Product and Intraday vs
 * overnight stay in the Breakdowns card above (unchanged), so no table appears twice.
 */
export const HOW_YOU_TRADE_OPTIONS: {
  key: HowYouTradeKey
  label: string
  column: string
  note: string
  /** Full explanation, shown as a tooltip on the note. */
  detail: string
}[] = [
  {
    key: 'time_of_day',
    label: 'Time of day',
    column: 'Entry time (IST)',
    note: 'By entry time (IST). Other: outside 09:15 to 15:30.',
    detail:
      'By the entry time of the position (IST). Each bucket includes its start and excludes its end. "Other" is outside NSE cash hours (before 09:15 or from 15:30: pre-open, MCX evening). It is not an error.',
  },
  {
    key: 'weekday',
    label: 'Weekday',
    column: 'Entry day',
    note: 'By entry session date.',
    detail:
      'By the session date of the entry. A weekend or holiday session is listed under its own weekday.',
  },
  {
    key: 'hold_bucket',
    label: 'Holding time',
    column: 'Held',
    note: 'By holding time. Overnight: closed in a later session.',
    detail:
      'By the holding time of the trade. A partial exit counts its money in its own bucket. Overnight: closed in a later session than it opened.',
  },
]

/** Human label for a "How you trade" bucket key. */
export function bucketLabel(key: string): string {
  const labels: Record<string, string> = {
    other: 'Other (outside NSE cash hours, 09:15-15:30)',
    '<5m': 'Under 5 min',
    '5-15m': '5-15 min',
    '15-60m': '15-60 min',
    '1-4h': '1-4 h',
    '>4h same day': 'Over 4 h, same day',
    overnight: 'Overnight',
    unknown: 'Unknown',
    Mon: 'Monday',
    Tue: 'Tuesday',
    Wed: 'Wednesday',
    Thu: 'Thursday',
    Fri: 'Friday',
    Sat: 'Saturday',
    Sun: 'Sunday',
  }
  return labels[key] ?? key
}

// ---------------------------------------------------------------- category, points, charts

export const CATEGORY_LABEL: Record<string, string> = {
  intraday: 'Intraday',
  swing: 'Swing',
  unclassified: 'Unclassified',
}

/** Why a position could not be called intraday or swing, in trader language. */
const CATEGORY_REASON_TEXT: Record<string, string> = {
  opening_unknown: 'the fill that opened it is not in the ledger',
  orphan_fills: 'some of its fills could not be placed',
  before_reliable_since: 'opened before the ledger became reliable',
  invalid_times: 'fill times are missing or out of order',
  fill_outside_session: 'a fill is outside the exchange trading hours',
  special_session: 'opened in a special trading session',
  still_open_in_session: 'still open in its opening session',
  fallback_boundary: 'the exchange calendar was not available to check the session end',
}

export function categoryReasonText(code: string | null | undefined): string {
  if (!code) return 'not classified'
  return CATEGORY_REASON_TEXT[code] ?? code.replace(/_/g, ' ')
}

/** Tooltip of an unclassified count: every reason with its count. */
export function unclassifiedReasonsText(reasons: Record<string, number> | undefined): string {
  const entries = Object.entries(reasons ?? {})
  if (entries.length === 0) return 'No reason recorded'
  return entries.map(([code, n]) => `${n}: ${categoryReasonText(code)}`).join('; ')
}

/** One line under the category selector: closed trades of the range per category. */
export function classificationLine(c: ClassificationBlock | null | undefined): string | null {
  if (!c) return null
  const k = c.closed
  return `Closed trades in this range: ${k.intraday} intraday, ${k.swing} swing, ${k.unclassified} unclassified.`
}

/** Unclassified positions count only in All trades; say so when a category is chosen. */
export function unclassifiedScopeNote(
  c: ClassificationBlock | null | undefined,
  category: DiscretionaryCategory
): string | null {
  if (!c || c.closed.unclassified <= 0) return null
  return category === 'all'
    ? `${c.closed.unclassified} unclassified position(s) are included in All trades only.`
    : `${c.closed.unclassified} unclassified position(s) show in All trades only.`
}

const STALE_COLLECTION_DAYS = 2

/** Calendar date (YYYY-MM-DD) and weekday (0 = Sunday) in India, whatever the browser's zone. */
function istToday(now: Date): { date: string; weekday: number } {
  const ist = new Date(now.getTime() + 5.5 * 3600 * 1000)
  return { date: ist.toISOString().slice(0, 10), weekday: ist.getUTCDay() }
}

function dayGap(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000)
}

/** Sync-run statuses that count as a completed sync (the server's SYNC_OK_STATUSES). */
const SYNC_OK = ['ok', 'partial']

/**
 * True when the newest day with sync runs in `sync_health.runs_by_day` holds only runs that did
 * not complete. A day with any completed run is not reported: the day counts carry no order, so
 * a failure after a success on the same day cannot be told apart from the reverse.
 */
export function lastSyncFailed(health: SyncHealthLike | null | undefined): boolean {
  const byDay = health?.runs_by_day
  if (!byDay) return false
  const latest = Object.keys(byDay)
    .filter((d) => Object.values(byDay[d] ?? {}).some((n) => n > 0))
    .sort()
    .pop()
  if (!latest) return false
  const statuses = Object.entries(byDay[latest]).filter(([, n]) => n > 0)
  return statuses.length > 0 && !statuses.some(([k]) => SYNC_OK.includes(k))
}

/**
 * Fills of the selected range left out of the trades: `unresolved_in_range.total`, or the pending
 * and unplaced counts of `data_quality` on an older server. 0 when the range has none.
 */
export function unmatchedFillCount(
  summary: Pick<DiscretionarySummary, 'unresolved_in_range' | 'data_quality'>
): number {
  const dq = summary.data_quality
  return (
    summary.unresolved_in_range?.total ??
    (dq ? (dq.awaiting_attribution ?? 0) + (dq.not_placed ?? 0) : 0)
  )
}

/** The quiet coverage note under the headline figures. */
export function unmatchedFillsNote(n: number): string {
  return `${n} ${n === 1 ? 'fill' : 'fills'} in this period ${n === 1 ? 'is' : 'are'} not counted, so P&L and trade count may be incomplete.`
}

/** Tooltip of the note: the technical reason, kept out of the line itself. */
export const UNMATCHED_FILLS_REASON =
  'These fills could not be matched to a manual trade, so they are left out of P&L, trade count and win rate.'

type SyncHealthLike = { runs_by_day?: Record<string, Record<string, number>> }

/**
 * The one warning shown when missing or doubtful source data changes the P&L of the SELECTED
 * report, in trader words. Null when nothing is wrong. Driven only by fields the server already
 * sends; each part is shown only when the problem touches the selected range:
 * - fills of the range left out of the trades are NOT here: they have their own quiet note
 *   (`unmatchedFillCount`, `unmatchedFillsNote`) under the headline figures;
 * - `data_quality.prices.missing` (open positions with no price, when prices were requested);
 * - `sync_health.days_without_sync` (weekdays of the range whose fills may be absent);
 * - `positions` that are MIS, opened on a day before today and still open (a closing fill is
 *   probably absent), when the range reaches the day they were opened;
 * - `positions` and `trades` flagged `opening_unknown` (entry price unknown);
 * - `sync_health.runs_by_day`: the newest day of sync runs holds no completed run (the last
 *   sync with the broker did not complete);
 * - `sync_health.last_successful_sync` older than two days on a weekday, when the range reaches
 *   past that date and the days above do not already name the gap.
 */
export function materialDataWarning(
  summary: Pick<DiscretionarySummary, 'unresolved_in_range' | 'data_quality' | 'sync_health'> &
    Partial<Pick<DiscretionarySummary, 'period' | 'positions' | 'trades'>>,
  now: Date = new Date()
): string | null {
  const dq = summary.data_quality
  const rangeStart = summary.period?.start?.slice(0, 10)
  const rangeEnd = summary.period?.end?.slice(0, 10)
  const noPrice = dq?.prices?.requested ? (dq.prices.missing ?? 0) : 0
  const days = (summary.sync_health?.days_without_sync ?? []).filter(
    (d) => (!rangeStart || d >= rangeStart) && (!rangeEnd || d <= rangeEnd)
  )
  const today = istToday(now)
  const positions = summary.positions ?? []
  const staleIntraday = positions.filter((p) => {
    const opened = p.opened_at?.slice(0, 10)
    return (
      p.product === 'MIS' && !!opened && opened < today.date && (!rangeEnd || opened <= rangeEnd)
    )
  }).length
  const unknownOpening =
    positions.filter(
      (p) => p.opening_unknown && (!rangeEnd || (p.opened_at ?? '').slice(0, 10) <= rangeEnd)
    ).length + (summary.trades ?? []).filter((t) => t.opening_unknown).length
  const lastSync = summary.sync_health?.last_successful_sync?.slice(0, 10)
  const staleSince =
    days.length === 0 &&
    lastSync &&
    today.weekday >= 1 &&
    today.weekday <= 5 &&
    dayGap(lastSync, today.date) > STALE_COLLECTION_DAYS &&
    (!rangeEnd || rangeEnd > lastSync)
      ? lastSync
      : null
  const parts: string[] = []
  if (lastSyncFailed(summary.sync_health)) {
    parts.push(
      'Your last sync with the broker did not complete. Recent trades may be missing. Use Refresh to try again.'
    )
  }
  if (noPrice > 0) {
    parts.push(
      `${noPrice} open ${noPrice === 1 ? 'position has' : 'positions have'} no current price, so open P&L may be incomplete.`
    )
  }
  if (days.length > 0) {
    const shown = days.slice(0, 3).join(', ')
    const more = days.length > 3 ? ` and ${days.length - 3} more` : ''
    parts.push(
      `Trades from ${shown}${more} may be missing, so P&L for those days may be incomplete.`
    )
  }
  if (staleSince) {
    parts.push(`Trades after ${staleSince} may be missing, so your P&L may be incomplete.`)
  }
  if (staleIntraday > 0) {
    parts.push(
      `${staleIntraday} intraday ${staleIntraday === 1 ? 'position is' : 'positions are'} still open from an earlier day, so a closing trade may be missing and your P&L may be incomplete.`
    )
  }
  if (unknownOpening > 0) {
    parts.push(
      unknownOpening === 1
        ? '1 trade or position has no recorded entry, so its P&L may be wrong.'
        : `${unknownOpening} trades or positions have no recorded entry, so their P&L may be wrong.`
    )
  }
  return parts.length > 0 ? parts.join(' ') : null
}

/** Points per unit with an explicit sign ("+12.50"); an em dash when unknown. */
export function fmtPoints(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  const text = Math.abs(value).toFixed(2)
  return value > 0 ? `+${text}` : value < 0 ? `-${text}` : text
}

/** Unsigned points (MAE / MFE are distances); an em dash when unknown. */
export function fmtDistance(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toFixed(2)
}

/** Rupees with an explicit sign, whatever the page's currency formatter does with negatives. */
export function signedMoney(value: number, formatCurrency: (v: number) => string): string {
  if (value > 0) return `+${formatCurrency(value)}`
  if (value < 0) return `-${formatCurrency(Math.abs(value))}`
  return formatCurrency(0)
}

/** A percentage as "12.3%"; an em dash when unknown. */
export function fmtPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return `${value.toFixed(1)}%`
}

/** Win / loss streak, e.g. "3 wins"; "none" when the last trade was break-even or there is none. */
export function streakText(streak: { type: 'win' | 'loss' | null; length: number } | undefined) {
  if (!streak || !streak.type || streak.length <= 0) return 'none'
  const noun = streak.type === 'win' ? 'win' : 'loss'
  return `${streak.length} ${noun}${streak.length === 1 ? '' : streak.type === 'win' ? 's' : 'es'}`
}

export interface NetEquity {
  points: { date: string; value: number }[]
  /** Trades that went into the curve, and those left out for want of a net P&L. */
  used: number
  skipped: number
}

/**
 * NET realised equity of the closed trades: cumulative net P&L (after estimated charges) in close
 * order (closed_at, then id), one point per close date holding the end-of-day value. A trade
 * without a net is skipped, never counted as zero.
 */
export function netEquitySeries(
  trades: Pick<DiscretionaryTrade, 'id' | 'closed_at' | 'trade_date' | 'net_pnl'>[]
): NetEquity {
  const ordered = [...trades].sort((a, b) =>
    a.closed_at === b.closed_at ? a.id - b.id : a.closed_at < b.closed_at ? -1 : 1
  )
  let equity = 0
  let used = 0
  let skipped = 0
  const byDate = new Map<string, number>()
  for (const t of ordered) {
    if (t.net_pnl == null || Number.isNaN(t.net_pnl)) {
      skipped += 1
      continue
    }
    used += 1
    equity += t.net_pnl
    byDate.set((t.trade_date || t.closed_at).slice(0, 10), Math.round(equity * 100) / 100)
  }
  return { points: [...byDate.entries()].map(([date, value]) => ({ date, value })), used, skipped }
}

export interface NetBar {
  key: string
  label: string
  value: number
  count: number
  /** Share of the largest absolute value, 0-100. */
  pct: number
}

function toBars(rows: { key: string; label: string; value: number; count: number }[]): NetBar[] {
  const max = rows.reduce((m, r) => Math.max(m, Math.abs(r.value)), 0)
  return rows.map((r) => ({ ...r, pct: max > 0 ? (Math.abs(r.value) / max) * 100 : 0 }))
}

/**
 * Monthly or weekday rows as net P&L bars (positions with a net are counted in `count`). A row
 * with no completed position has no bar: it is a gap, never a bar of zero.
 */
export function calendarBars(rows: CalendarRow[] | null | undefined): NetBar[] {
  return toBars(
    (rows ?? [])
      .filter((r) => r.positions > 0)
      .map((r) => ({
        key: r.key,
        label: r.key,
        value: r.net_of_charges,
        count: r.positions - r.net_unavailable_positions,
      }))
  )
}

/** The by-symbol breakdown as net P&L bars, best first. */
export function symbolBars(rows: BreakdownRow[] | null | undefined): NetBar[] {
  return toBars(
    [...(rows ?? [])]
      .sort((a, b) => b.net_of_charges - a.net_of_charges)
      .map((r) => ({
        key: r.key ?? '',
        label: r.key ?? '',
        value: r.net_of_charges,
        count: r.trades,
      }))
  )
}

export type ScatterKind = 'winner' | 'loser' | 'even'

export interface ScatterPoint {
  id: number
  symbol: string
  mae: number
  mfe: number
  kind: ScatterKind
  net: number | null
  direction?: 'LONG' | 'SHORT'
  opened_at?: string
  closed_at?: string
}

/** Available positions with both MAE and MFE; unavailable ones are never plotted (never at 0). */
export function scatterPoints(positions: ExcursionPosition[] | undefined): ScatterPoint[] {
  const out: ScatterPoint[] = []
  for (const p of positions ?? []) {
    if (p.status !== 'available' || p.mae_points == null || p.mfe_points == null) continue
    const kind: ScatterKind =
      p.net_pnl == null || p.net_pnl === 0 ? 'even' : p.net_pnl > 0 ? 'winner' : 'loser'
    out.push({
      id: p.id,
      symbol: p.symbol,
      mae: p.mae_points,
      mfe: p.mfe_points,
      kind,
      net: p.net_pnl,
      direction: p.direction,
      opened_at: p.opened_at,
      closed_at: p.closed_at,
    })
  }
  return out
}

/** Excursions by position id, only when they come from the same build as the summary rows. */
export function excursionIndex(
  excursions: ExcursionsResponse | undefined,
  summaryBuildId: string | null | undefined
): Map<number, ExcursionPosition> {
  const map = new Map<number, ExcursionPosition>()
  if (!excursions || (summaryBuildId && excursions.build_id !== summaryBuildId)) return map
  for (const p of excursions.positions) map.set(p.id, p)
  return map
}

/** "TCS (NSE)": an instrument is symbol plus exchange. */
export function instrumentLabel(symbol: string | null, exchange: string | null): string {
  return `${symbol ?? 'unknown'}${exchange ? ` (${exchange})` : ''}`
}

/** A fraction as a whole percent ("62%"); an em dash when unknown. Capture is a gross-points ratio. */
export function fmtCapture(fraction: number | null | undefined): string {
  if (fraction == null || Number.isNaN(fraction)) return '—'
  return `${(fraction * 100).toFixed(0)}%`
}

const CAPTURE_REASON_WORDS: Record<string, string> = {
  net_unavailable: 'net result unknown (no charges)',
  not_net_winner: 'not a net winner',
  excursion_unavailable: 'price data not available',
  insufficient_candle_coverage: 'no full candle between first and last fill',
  mfe_not_positive: 'no favourable move recorded',
  data_inconsistency: 'exit outside the recorded price path',
}

/** Why positions are outside the capture figures: ["6 not a net winner", ...]. */
export function captureExclusionPhrases(
  excluded: Partial<Record<string, number>> | null | undefined
): string[] {
  return Object.entries(excluded ?? {})
    .filter(([, n]) => (n ?? 0) > 0)
    .map(([code, n]) => `${n} ${CAPTURE_REASON_WORDS[code] ?? code.replace(/_/g, ' ')}`)
}

/** Distinct instruments among the positions that have MAE / MFE (what pooled points would mix). */
export function availableInstrumentCount(positions: ExcursionPosition[] | undefined): number {
  const keys = new Set<string>()
  for (const p of positions ?? []) {
    if (p.status === 'available') keys.add(`${p.symbol}|${p.exchange}`)
  }
  return keys.size
}

// ---------------------------------------------------------------- per-position equity curve

/** Money below half a paise is float noise, never a result (the backend's clean_money rule). */
const PAISE_EPS = 0.005
export function cleanMoney(value: number): number {
  return Math.abs(value) < PAISE_EPS ? 0 : value
}

export interface CurvePoint {
  /** 1-based position number in close order (the x axis; no timestamps are invented). */
  index: number
  id: number
  /** Close time of the position (IST text), or the close date for an end-of-day point. */
  closed_at: string
  symbol: string | null
  direction: 'LONG' | 'SHORT' | null
  /** Net P&L of this position (after estimated charges), or the day's change for a day point. */
  net: number
  /** Cumulative net P&L after this point. */
  equity: number
  /** Highest cumulative net so far; starts at 0. */
  peak: number
  /** Peak minus equity, in rupees (0 at a new high). */
  drawdown: number
  /** A day point whose figure is not complete (no charge estimate, or trades not collected). */
  incomplete?: boolean
}

export interface NetCurve {
  points: CurvePoint[]
  used: number
  skipped: number
  maxDrawdown: number
  maxDrawdownAt: string | null
  currentDrawdown: number
  peakEquity: number
  latestEquity: number
}

function sortByClose<T extends { id: number; closed_at: string }>(rows: T[]): T[] {
  // The backend sorts by (str(closed_at), id): plain string order, ties by id.
  return [...rows].sort((a, b) =>
    a.closed_at === b.closed_at ? a.id - b.id : a.closed_at < b.closed_at ? -1 : 1
  )
}

/**
 * NET realised equity per closed position, with the drawdown defined exactly as the server's
 * Risk card (services/discretionary_analytics.py drawdown()): equity and peak start at 0,
 * positions run in (closed_at, id) order, a position without a net is skipped (counted, never
 * zero), each step adds the net cleaned of sub-paise noise, drawdown = peak - equity.
 * Displayed values are the cleaned nets; nothing is rounded here.
 */
export function netEquityPerTrade(
  trades: Pick<DiscretionaryTrade, 'id' | 'closed_at' | 'symbol' | 'direction' | 'net_pnl'>[]
): NetCurve {
  let equity = 0
  let peak = 0
  let worst = 0
  let worstAt: string | null = null
  let used = 0
  let skipped = 0
  const points: CurvePoint[] = []
  for (const t of sortByClose(trades)) {
    if (t.net_pnl == null || !Number.isFinite(t.net_pnl)) {
      skipped += 1
      continue
    }
    const net = cleanMoney(t.net_pnl)
    used += 1
    equity += net
    peak = Math.max(peak, equity)
    const drawdown = peak - equity
    if (drawdown > worst) {
      worst = drawdown
      worstAt = t.closed_at
    }
    points.push({
      index: used,
      id: t.id,
      closed_at: t.closed_at,
      symbol: t.symbol,
      direction: t.direction,
      net,
      equity,
      peak,
      drawdown,
    })
  }
  return {
    points,
    used,
    skipped,
    maxDrawdown: worst,
    maxDrawdownAt: worstAt,
    currentDrawdown: peak - equity,
    peakEquity: peak,
    latestEquity: equity,
  }
}

/** The end-of-day curve (netEquitySeries points) with the same peak-from-0 drawdown rule. */
export function netEquityDaily(points: { date: string; value: number }[]): NetCurve {
  let peak = 0
  let worst = 0
  let worstAt: string | null = null
  let prev = 0
  const out: CurvePoint[] = points.map((p, i) => {
    peak = Math.max(peak, p.value)
    const drawdown = peak - p.value
    if (drawdown > worst) {
      worst = drawdown
      worstAt = p.date
    }
    const point: CurvePoint = {
      index: i + 1,
      id: i + 1,
      closed_at: p.date,
      symbol: null,
      direction: null,
      net: p.value - prev,
      equity: p.value,
      peak,
      drawdown,
    }
    prev = p.value
    return point
  })
  const latest = points.length > 0 ? points[points.length - 1].value : 0
  return {
    points: out,
    used: out.length,
    skipped: 0,
    maxDrawdown: worst,
    maxDrawdownAt: worstAt,
    currentDrawdown: peak - latest,
    peakEquity: peak,
    latestEquity: latest,
  }
}

/**
 * The Equity Curve: cumulative sum of the server's daily realised NET P&L rows (by_period.daily,
 * after estimated charges), one point per day that has a row, oldest first. Days without a row
 * (weekends, holidays, no activity) are not points, and no value is invented for them. The only
 * arithmetic is the running sum; the drawdown rule is the one of netEquityDaily (peak from 0).
 * `incompleteDays` counts days whose net lacks some charge estimates.
 * The server rounds every day to 2 decimals, so the running sum of the days can sit a paisa or
 * two from the period net (2,823.83 against 2,823.8226). When `periodNet` (the same response's
 * realised net after charges) is given and the sum is within that rounding drift, the last point
 * is the period net rounded once, so the chart and the headline show the same figure.
 */
export function netEquityFromDailyRows(
  rows: PeriodRow[] | null | undefined,
  notCollected: readonly string[] = [],
  periodNet?: number | null
): NetCurve & {
  incompleteDays: number
} {
  const ordered = [...(rows ?? [])]
    .filter((r) => Number.isFinite(r.net_of_charges))
    .sort((a, b) => (a.period < b.period ? -1 : a.period > b.period ? 1 : 0))
  let equity = 0
  let peak = 0
  let worst = 0
  let worstAt: string | null = null
  let incompleteDays = 0
  const runningTotal = ordered.reduce((s, r) => s + cleanMoney(r.net_of_charges), 0)
  const target =
    periodNet != null && Number.isFinite(periodNet) ? Math.round(periodNet * 100) / 100 : null
  const snapLast =
    target != null &&
    ordered.length > 0 &&
    Math.abs(runningTotal - target) <= 0.01 * ordered.length + 1e-9
  const points: CurvePoint[] = ordered.map((r, i) => {
    const net = cleanMoney(r.net_of_charges)
    equity = Math.round((equity + net) * 100) / 100
    if (snapLast && target != null && i === ordered.length - 1) equity = target
    peak = Math.max(peak, equity)
    const drawdown = peak - equity
    if (drawdown > worst) {
      worst = drawdown
      worstAt = r.period
    }
    const chargesMissing = netIncomplete(r)
    if (chargesMissing) incompleteDays += 1
    return {
      index: i + 1,
      id: i + 1,
      closed_at: r.period,
      symbol: null,
      direction: null,
      net,
      equity,
      peak,
      drawdown,
      // trades not collected on a day with a row: the figure is shown, but flagged
      incomplete: chargesMissing || notCollected.includes(r.period),
    }
  })
  return {
    points,
    used: points.length,
    skipped: 0,
    maxDrawdown: worst,
    maxDrawdownAt: worstAt,
    currentDrawdown: peak - equity,
    peakEquity: peak,
    latestEquity: equity,
    incompleteDays,
  }
}

/** Basis sentence under the Equity Curve. */
export function equityCurveBasis(curve: { used: number; incompleteDays: number }): string {
  const days = `${curve.used} trading day${curve.used === 1 ? '' : 's'} with realised P&L`
  const incomplete =
    curve.incompleteDays > 0
      ? ` ${curve.incompleteDays} day(s) include trades without a charge estimate.`
      : ''
  return `Cumulative net P&L after estimated charges, over ${days}.${incomplete}`
}

export interface EquityReconciliation {
  kind: 'match' | 'mismatch'
  text: string
}

/** Ties the last point of the curve to the period's realised net P&L from the same response. */
export function equityCurveReconciliation(
  curve: { latestEquity: number },
  periodNet: number,
  money: (v: number) => string,
  scope = 'the selected period'
): EquityReconciliation {
  const diff = Math.abs(curve.latestEquity - periodNet)
  if (diff <= 0.01) {
    return {
      kind: 'match',
      text: `The last point (${money(curve.latestEquity)}) equals the realised net P&L of ${scope}.`,
    }
  }
  return {
    kind: 'mismatch',
    text: `The last point (${money(curve.latestEquity)}) differs from the net P&L of ${scope} (${money(periodNet)}). Trades without a charge estimate are incomplete in both.`,
  }
}

/** Entry-time buckets with trades as net P&L bars, in the server's time order; label = bucket and trade count. */
export function timeOfDayBars(rows: BreakdownRow[] | null | undefined): NetBar[] {
  return toBars(
    (rows ?? [])
      .filter((r) => r.trades > 0)
      .map((r) => ({
        key: r.key ?? '',
        // the count in brackets keeps the bar readable without the table
        label: `${r.key === 'other' ? 'Other (outside NSE hours)' : (r.key ?? '')} (${r.trades})`,
        value: r.net_of_charges,
        count: r.trades,
      }))
  )
}

export type ReconcileKind =
  | 'match'
  | 'truncated'
  | 'mismatch'
  | 'no_risk'
  | 'day_curve'
  | 'day_no_risk'

export interface Reconciliation {
  kind: ReconcileKind
  text: string
}

const DD_TOLERANCE = 0.01

/**
 * The caption that ties the chart's maximum drawdown to the Risk figure, computed at run time from
 * the numbers themselves (it only labels; no displayed figure is changed). Never leaves a
 * difference unexplained: a cut trade list, positions without a net and a plain mismatch each
 * say which basis the curve and the Risk card use.
 */
export function drawdownReconciliation(args: {
  curve: NetCurve
  mode: 'position' | 'day'
  tradesShown: number
  tradesTotal: number
  risk: RiskBlock | null | undefined
  formatCurrency: (v: number) => string
}): Reconciliation {
  const { curve, mode, tradesShown, tradesTotal, risk, formatCurrency: money } = args
  const curveDd = money(curve.maxDrawdown)
  // The day chart is drawn from end-of-day equity; the Risk figure runs over each closed position
  // in close order, so it can include a fall inside a day. Say so only when the two differ.
  if (mode === 'day') {
    if (!risk) return { kind: 'day_no_risk', text: '' }
    if (Math.abs(curve.maxDrawdown - risk.max_drawdown) <= DD_TOLERANCE) {
      return { kind: 'match', text: '' }
    }
    return {
      kind: 'day_curve',
      text: 'Drawdown pane uses end-of-day totals; Max drawdown uses each closed trade.',
    }
  }
  if (!risk) return { kind: 'no_risk', text: '' }
  if (tradesTotal > tradesShown) {
    return {
      kind: 'truncated',
      text: `Chart drawdown (${curveDd}) covers the latest ${tradesShown} of ${tradesTotal} trades; Max drawdown (${money(risk.max_drawdown)}) covers all.`,
    }
  }
  const same =
    Math.abs(curve.maxDrawdown - risk.max_drawdown) <= DD_TOLERANCE &&
    Math.abs(curve.currentDrawdown - risk.current_drawdown) <= DD_TOLERANCE &&
    curve.skipped === risk.net_unavailable_positions
  if (same) {
    return { kind: 'match', text: '' }
  }
  return {
    kind: 'mismatch',
    text: `Chart drawdown (${curveDd}) counts ${curve.used} ${curve.used === 1 ? 'trade' : 'trades'}; Max drawdown (${money(risk.max_drawdown)}) counts ${risk.positions}. Trades without net P&L are left out.`,
  }
}

// ---------------------------------------------------------------- section visibility by data

export type ProfileName = 'session' | 'short' | 'medium' | 'long'

export interface PeriodProfile {
  profile: ProfileName
  showByPeriodTabs: boolean
  /** Which tabs of "P&L by period" are offered, in order. Days are the calendar's, so no Daily tab. */
  byPeriodTabs: ('weekly' | 'monthly')[]
  /** The curve is one point per closed position, for 7 or fewer close dates; else end-of-day. */
  perPositionCurve: boolean
  /** One visible line naming what is hidden and why; null when nothing is. */
  note: string | null
}

/**
 * Which sections make sense for the data on screen. `closeDates` = distinct dates with a closed
 * position; `months` = number of calendar months in the monthly rows. A 1-day period is always
 * the session profile. One cumulative-net curve is shown: per closed trade for 7 or fewer close
 * dates, per day beyond that.
 */
export function periodProfile(input: {
  period: string
  closeDates: number
  months: number
}): PeriodProfile {
  const { period, closeDates, months } = input
  if (period === '1d' || closeDates <= 1) {
    return {
      profile: 'session',
      showByPeriodTabs: false,
      byPeriodTabs: [],
      perPositionCurve: true,
      note: 'Weekly and monthly tables are hidden for one trading day.',
    }
  }
  if (closeDates > 31 || months >= 3) {
    return {
      profile: 'long',
      showByPeriodTabs: true,
      byPeriodTabs: ['weekly', 'monthly'],
      perPositionCurve: false,
      note: null,
    }
  }
  if (closeDates <= 7) {
    return {
      profile: 'short',
      showByPeriodTabs: false,
      byPeriodTabs: [],
      perPositionCurve: true,
      note: `Weekly and monthly tables are hidden: only ${closeDates} trading days.`,
    }
  }
  const monthly = months >= 2
  return {
    profile: 'medium',
    showByPeriodTabs: true,
    byPeriodTabs: monthly ? ['weekly', 'monthly'] : ['weekly'],
    perPositionCurve: false,
    note: monthly ? null : 'Monthly table is hidden: range is within one month.',
  }
}

/** Distinct close dates: server daily rows with closed positions, else the shown trades' dates. */
export function closeDateCount(
  summary: Pick<DiscretionarySummary, 'by_period' | 'trades'> | undefined
): number {
  const daily = (summary?.by_period?.daily ?? []).filter((r) => r.trades > 0)
  if (daily.length > 0) return daily.length
  return new Set((summary?.trades ?? []).map((t) => (t.trade_date || t.closed_at).slice(0, 10)))
    .size
}

/**
 * Hint under the Charges tile when charges matter: they turned a gross profit into a net loss,
 * or take 30% or more of the gross profit of the winning trades. Facts from fields the server
 * already sends, each with the number of completed trades it rests on (no minimum count). Null
 * when neither applies. A description of the results, not advice.
 */
export function chargesInsightHint(
  summary: Pick<
    DiscretionarySummary,
    'realized_gross' | 'realized_net_of_charges' | 'metrics' | 'costs'
  >,
  formatCurrency: (v: number) => string
): string | null {
  const count = summary.metrics.trades_count
  if (count === 0) return null
  const net = cleanMoney(summary.realized_net_of_charges)
  const sampleText = `over ${count} completed trade${count === 1 ? '' : 's'}`
  if (summary.realized_gross > 0 && net < 0) {
    return `turned a gross profit of ${formatCurrency(summary.realized_gross)} into a net loss, ${sampleText}`
  }
  const pct = summary.costs?.charges_pct_of_gross_profit
  if (pct != null && pct >= 30) {
    return `${pct.toFixed(1)}% of the gross profit of your winning trades, ${sampleText}`
  }
  return null
}

/**
 * Hint under the Largest loser tile: the worst trade is larger than the best, or no trade won.
 * Net only: a trade without a net P&L is unavailable, never compared on gross. Null otherwise.
 */
export function largestLossHint(
  summary: Pick<DiscretionarySummary, 'metrics' | 'consistency'>,
  formatCurrency: (v: number) => string
): string | null {
  const count = summary.metrics.trades_count
  if (count === 0) return null
  const sampleText = `over ${count} completed trade${count === 1 ? '' : 's'}`
  const win = summary.consistency?.largest_win
  const loss = summary.consistency?.largest_loss
  if (win && loss) {
    if (win.net_pnl != null && loss.net_pnl != null) {
      const w = win.net_pnl
      const l = Math.abs(loss.net_pnl)
      if (l > w) {
        return `larger than your largest winner (${formatCurrency(w)}), ${sampleText}`
      }
    }
  } else if (loss && !win && loss.net_pnl != null) {
    return `none of your ${count} completed trade${count === 1 ? '' : 's'} won`
  }
  return null
}

// ---------------------------------------------------------------- trade-by-trade progression

/**
 * One plain-English reading of the trade-by-trade curve: where it finished, how many trades won
 * and lost, its highest point and its largest fall from a peak. Facts from the plotted points
 * only; no judgement about the trader.
 */
export function progressionText(
  curve: NetCurve,
  mode: 'position' | 'day',
  money: (v: number) => string
): string {
  const pts = curve.points
  if (pts.length === 0) return ''
  const unit = mode === 'position' ? 'trade' : 'day'
  const n = pts.length
  const wins = pts.filter((p) => p.net > 0).length
  const losses = pts.filter((p) => p.net < 0).length
  const last = pts[n - 1].equity
  const high = pts.reduce((a, p) => (p.equity > a.equity ? p : a), pts[0])
  const deep = pts.reduce((a, p) => (p.drawdown > a.drawdown ? p : a), pts[0])
  const parts = [
    `Net P&L finished at ${money(last)} after ${n} ${unit}${n === 1 ? '' : 's'}: ${wins} winning, ${losses} losing.`,
    high.equity > 0
      ? `The highest point was ${money(high.equity)} after ${unit} ${high.index}.`
      : 'It never rose above zero.',
    deep.drawdown > 0
      ? `The largest fall from a peak was ${money(deep.drawdown)}, reached after ${unit} ${deep.index}.`
      : 'It never fell below an earlier peak.',
  ]
  return parts.join(' ')
}

// ---------------------------------------------------------------- breakdown visibility

export type BreakdownTabKey =
  | 'symbol'
  | 'side'
  | 'time_of_day'
  | 'weekday'
  | 'hold_bucket'
  | 'product'
  | 'holding'

/**
 * The tabs of the one "Where and when" card, in order. `table` says which existing table shows
 * the rows: `how` (trades, win rate, average net P&L, profit factor, gross, charges, net) or
 * `group` (trades, win rate, profit factor, average hold, gross, net).
 */
export const BREAKDOWN_TABS: {
  key: BreakdownTabKey
  label: string
  column: string
  table: 'how' | 'group'
  note: string
  detail?: string
}[] = [
  {
    key: 'symbol',
    label: 'Symbol',
    column: 'Symbol',
    table: 'group',
    note: 'By symbol, closed trades.',
  },
  {
    key: 'side',
    label: 'Long / Short',
    column: 'Side',
    table: 'group',
    note: 'By direction of the trade.',
  },
  ...HOW_YOU_TRADE_OPTIONS.map((o) => ({ ...o, table: 'how' as const })),
  {
    key: 'product',
    label: 'Product',
    column: 'Product',
    table: 'group',
    note: 'By product (MIS, NRML, CNC).',
  },
  {
    key: 'holding',
    label: 'Intraday vs overnight',
    column: 'Holding',
    table: 'group',
    note: 'Intraday: closed in the session it opened. Overnight: closed in a later session.',
  },
]

export interface BreakdownVisibility {
  symbol: boolean
  side: boolean
  product: boolean
  holding: boolean
  points: boolean
  /** "How you trade" tabs (entry time, weekday, holding time) that have something to compare. */
  patterns: HowYouTradeKey[]
  /** Every tab of the one breakdown card that has something to compare, in order. */
  tabs: BreakdownTabKey[]
  /** True when no tab of the breakdown card is shown. */
  noneShown: boolean
}

function populatedRows(rows: BreakdownRow[] | undefined): number {
  return (rows ?? []).filter((r) => r.trades > 0).length
}

/** The rows behind one breakdown tab. */
export function breakdownTabRows(
  summary: DiscretionarySummary,
  key: BreakdownTabKey
): BreakdownRow[] {
  switch (key) {
    case 'symbol':
      return summary.by_symbol ?? []
    case 'side':
      return summary.by_side ?? []
    case 'product':
      return summary.by_product ?? []
    case 'holding':
      return holdingRows(summary.by_holding)
    default:
      return summary.breakdowns?.[key] ?? []
  }
}

/**
 * Which analytical breakdowns have a real comparison in them. One symbol, one product or one
 * direction compares nothing against nothing, so at least two groups are needed. Product and
 * Intraday vs overnight are tabs only when they have two or more groups.
 */
export function breakdownVisibility(summary: DiscretionarySummary): BreakdownVisibility {
  const patterns = HOW_YOU_TRADE_OPTIONS.map((o) => o.key).filter(
    (k) => populatedRows(summary.breakdowns?.[k]) > 1
  )
  const symbol = populatedRows(summary.by_symbol) > 1
  const side = populatedRows(summary.by_side) > 1
  const product = populatedRows(summary.by_product) > 1
  const holding = populatedRows(holdingRows(summary.by_holding)) > 1
  const tabs = BREAKDOWN_TABS.map((t) => t.key).filter(
    (k) => populatedRows(breakdownTabRows(summary, k)) > 1
  )
  return {
    symbol,
    side,
    product,
    holding,
    points: (summary.points_by_instrument?.length ?? 0) > 1,
    patterns,
    tabs,
    noneShown: tabs.length === 0,
  }
}

// ---------------------------------------------------------------- month table (position basis)

/** At least two months with a completed position: the month table has something to compare. */
export function monthTableVisible(rows: CalendarRow[] | null | undefined): boolean {
  return (rows ?? []).filter((r) => r.positions > 0).length > 1
}

/**
 * Whether the month table's total differs from the headline net by more than 0.01. The table
 * counts a position once, on the month of its final close, so partial exits of a position that
 * is still open are in the headline but not in the table.
 */
export function monthlyTotalDiffers(
  rows: CalendarRow[] | null | undefined,
  headlineNet: number | null | undefined
): boolean {
  if (!rows || rows.length === 0 || typeof headlineNet !== 'number') return false
  const total = rows.reduce((sum, r) => sum + r.net_of_charges, 0)
  return Math.abs(total - headlineNet) > 0.01
}

export const MONTHLY_TABLE_NOTE =
  'Counted by final close month; the total can differ from net P&L above.'
export const MONTHLY_TABLE_DETAIL =
  'This table counts completed positions by the month of their final close. Partial exits of positions that are still open are not included, so its total can differ from the net P&L above and from the calendar.'

// ---------------------------------------------------------------- reliable start

const FIXED_WINDOW_DAYS: Record<string, number> = { '7d': 7, '15d': 15, '30d': 30, '90d': 90 }

function shiftIsoDate(date: string, days: number): string {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10)
}

/**
 * The start date the trader asked for, before the server moved it up to the first reliable
 * date: a custom range's own start, or the rolling window or year start worked out from today.
 * Null when no start is asked for ('1d' is the last session and 'all' starts at the first
 * trade, so neither can reach back past the floor).
 */
export function requestedStartDate(
  period: string,
  range: { start: string; end: string } | undefined,
  today: string
): string | null {
  if (period === 'custom') return range?.start ?? null
  if (period === 'ytd') return `${today.slice(0, 4)}-01-01`
  const days = FIXED_WINDOW_DAYS[period]
  return days ? shiftIsoDate(today, -(days - 1)) : null
}

/** The notice shown when the requested start is earlier than the first reliable date. */
export function reliableStartNotice(
  requestedStart: string | null | undefined,
  reliableSince: string | null | undefined,
  formatDate: (iso: string) => string = (iso) => iso
): string | null {
  if (!requestedStart || !reliableSince) return null
  if (requestedStart >= reliableSince) return null
  return `Your ledger is reliable from ${formatDate(reliableSince)}. Earlier dates are not included.`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function parseIsoDay(iso: string | null | undefined): { y: number; m: number; d: number } | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? '')
  if (!match) return null
  const [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])]
  return m >= 1 && m <= 12 ? { y, m, d } : null
}

/** '19 Nov' (or '19 Nov 2026' with `withYear`) from an ISO date; the input when unparsable. */
export function fmtDayMonth(iso: string | null | undefined, withYear = false): string {
  const p = parseIsoDay(iso)
  return p ? `${p.d} ${MONTHS[p.m - 1]}${withYear ? ` ${p.y}` : ''}` : (iso ?? '')
}
