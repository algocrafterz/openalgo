import type {
  BreakdownRow,
  CalendarRow,
  ClassificationBlock,
  DataQuality,
  DiscretionaryCategory,
  DiscretionaryPosition,
  DiscretionarySummary,
  DiscretionaryToday,
  DiscretionaryTrade,
  ExcursionPosition,
  ExcursionsResponse,
  PendingAttribution,
  PeriodRow,
  PriceStatus,
  RiskBlock,
  SampleContext,
  SyncHealth,
  UnresolvedInRange,
} from '@/api/discretionaryPnl'

/** The Performance page source: current-mode strategies (unchanged) or discretionary trading. */
export type PageSource = 'strategies' | 'discretionary'

export type SourceAvailability = 'pending' | 'enabled' | 'disabled'

/** URL value that selects the Discretionary source. Absent or anything else means strategies. */
export const DISCRETIONARY_SOURCE = 'discretionary'

/** Mirrors the Performance page's poll interval; quotes=1 polls at the same pace. */
export const DISCRETIONARY_POLL_MS = 60_000

export const NET_EQUITY_CAPTION =
  'Net realised P&L after estimated charges, end-of-day value by close date; excludes unrealized P&L. The drawdown strip is end-of-day too, so it can differ from the per-trade drawdown in Risk and consistency'
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
  losses: number
): string {
  if (value != null && Number.isFinite(value)) return value.toFixed(2)
  if (wins > 0 && losses === 0) return '∞'
  return '—'
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

/**
 * Backend lines that only restate what a card or the sync line already shows: the last-sync
 * stamp, the missing-sync days and "never synced" (all from sync_health), and "not requested"
 * (the Unrealized card). A prefix that stops matching only makes a line appear twice.
 */
const INFORMATIONAL_WARNING_PREFIXES = [
  'last successful sync:',
  'no successful ledger sync',
  'unrealized P&L not requested',
]

/**
 * The backend's ledger-wide line ("N unresolved fill(s) are excluded from P&L until
 * attributed."). It counts the whole ledger, not the selected range.
 */
const LEDGER_UNRESOLVED_WARNING = /^\d+ unresolved fill\(s\) are excluded from P&L/

/**
 * Backend warnings worth a banner: informational lines are left to the cards and the sync line.
 * When the server reports the in-range unresolved count (``unresolvedInRange`` present) the
 * ledger-wide unresolved line is dropped: the range banner says the same thing for the selected
 * range, and the ledger-wide count misleads on a narrow range. An older server (no
 * ``unresolved_in_range``) keeps the line. Every other warning is kept as is.
 */
export function actionableWarnings(
  warnings: string[] | undefined,
  unresolvedInRange?: UnresolvedInRange | null
): string[] {
  return (warnings ?? []).filter(
    (w) =>
      !INFORMATIONAL_WARNING_PREFIXES.some((prefix) => w.startsWith(prefix)) &&
      !(unresolvedInRange != null && LEDGER_UNRESOLVED_WARNING.test(w))
  )
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
}

/** Plain text for a raw reason or attribution code; unknown codes are shown with spaces. */
export function reasonText(code: string | null | undefined): string {
  if (!code) return '—'
  return REASON_TEXT[code] ?? code.replace(/_/g, ' ')
}

/** Plain-text net definition (the server sends field names; the page shows what they mean). */
export function netDefinitionText(raw: string | undefined): string {
  if (!raw) return ''
  return 'Net = gross realized minus estimated charges. Gross realized is before charges; win rate, profit factor, expectancy and the averages are measured on net P&L.'
}

/** True when a net figure leaves out charges it could not estimate. */
export function netIncomplete(row: {
  net_charges_complete?: boolean
  charges_unavailable_trades?: number
}): boolean {
  return row.net_charges_complete === false || (row.charges_unavailable_trades ?? 0) > 0
}

/** One line describing the ledger sync, and whether it needs attention. */
export function syncSummary(health: SyncHealth | undefined): { text: string; problem: boolean } {
  if (!health || !health.last_successful_sync) {
    return { text: 'Ledger sync: no successful sync yet', problem: true }
  }
  const missing = health.days_without_sync ?? []
  const base = `Ledger sync: last successful ${health.last_successful_sync} IST`
  if (missing.length === 0) return { text: base, problem: false }
  const shown = missing.slice(0, 5).join(', ')
  const more = missing.length > 5 ? ` and ${missing.length - 5} more` : ''
  return { text: `${base}; no sync on ${shown}${more}`, problem: true }
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
  return `No attributable trades available for performance analysis yet. ${collected} fills collected; ${unresolved} awaiting attribution. Their P&L is excluded until classification is resolved.`
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
    let text = `${held} execution(s) awaiting attribution: they are inside the ${pending.grace_minutes}-minute safety window that lets a strategy order tag arrive, and are not counted in P&L yet.`
    if (waiting > 0 && pending.earliest_attributable_at) {
      const next = (pending.grace_fills ?? []).find((f) => !f.due)
      text += ` Earliest attribution at ${fmtClock(next?.attributable_from ?? pending.earliest_attributable_at)} IST, at the first ledger sync after it.`
    }
    if (due > 0) text += ` ${due} passed the window and wait only for the next ledger sync.`
    if ((pending.grace_fills ?? []).some((f) => f.affects_open_position)) {
      text += ' Some belong to an open position, so its quantity may still change.'
    }
    lines.push(text)
  }
  if ((pending.other_today ?? 0) > 0) {
    lines.push(
      `${pending.other_today} execution(s) of today stay unresolved for another reason and are excluded from P&L; see the unresolved fills warning.`
    )
  }
  return lines
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

export const DAILY_CHART_CAPTION =
  'Daily realized P&L, gross (before charges): the same basis as the equity curve. Hover a bar for its estimated charges and net.'

export interface DailyBar {
  date: string
  gross: number
  charges: number
  net: number
  netComplete: boolean
  /** Bar length as a percent of the largest absolute day (0-100). */
  pct: number
}

/** Bars of the daily P&L chart (one per day with a close) and the day counts behind them. */
export function dailyBars(rows: PeriodRow[] | undefined): {
  bars: DailyBar[]
  profitable: number
  losing: number
  flat: number
} {
  const list = rows ?? []
  const peak = Math.max(0, ...list.map((r) => Math.abs(r.realized_gross)))
  const bars = list.map((r) => ({
    date: r.period,
    gross: r.realized_gross,
    charges: r.charges,
    net: r.net_of_charges,
    netComplete: !netIncomplete(r),
    pct: peak > 0 ? (Math.abs(r.realized_gross) / peak) * 100 : 0,
  }))
  return {
    bars,
    profitable: list.filter((r) => r.realized_gross > 0).length,
    losing: list.filter((r) => r.realized_gross < 0).length,
    flat: list.filter((r) => r.realized_gross === 0).length,
  }
}

/** "based on N completed trades over D trading days" plus a low-sample note by the server's rule. */
export function sampleNote(
  sample: SampleContext | undefined
): { text: string; low: boolean } | null {
  if (!sample) return null
  const base = `Ratios based on ${sample.completed_trades} completed trade(s) over ${sample.trading_days} trading day(s).`
  if (sample.sufficient) return { text: base, low: false }
  return {
    text: `${base} Low sample: fewer than ${sample.min_trades} completed trades or ${sample.min_days} trading days, so win rate, profit factor and expectancy are indicative only.`,
    low: true,
  }
}

/** Whether a row's ratios rest on a sample below the edge-check rule (server flag, else sample_ok). */
export function isLowSample(row: Pick<BreakdownRow, 'low_sample' | 'sample_ok'>): boolean {
  if (typeof row.low_sample === 'boolean') return row.low_sample
  return row.sample_ok === false
}

/**
 * Profit factor of a breakdown row: always the number (dash only when it is null), with
 * `low` set when the group is below the sample rule so the UI adds a muted "low sample" marker.
 */
export function rowProfitFactor(
  row: Pick<
    BreakdownRow,
    'trades' | 'profit_factor' | 'wins' | 'losses' | 'sample_ok' | 'low_sample'
  >,
  sample?: Pick<SampleContext, 'min_trades' | 'min_days'>
): { value: string; low: boolean; title?: string } {
  if (row.trades === 0) return { value: '—', low: false, title: 'No completed trade in this group' }
  const low = isLowSample(row)
  const rule = sample
    ? `at least ${sample.min_trades} completed trades over ${sample.min_days} trading days`
    : 'a larger sample'
  return {
    value: fmtProfitFactor(row.profit_factor, row.wins, row.losses),
    low,
    title: low
      ? `Low sample: indicative only; needs ${rule} (this group has ${row.trades})`
      : undefined,
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
}[] = [
  {
    key: 'time_of_day',
    label: 'Time of day',
    column: 'Entry time (IST)',
    note: 'By the entry time of the position (IST). Each bucket includes its start and excludes its end; "other" is outside 09:15-15:30 (pre-open, MCX evening).',
  },
  {
    key: 'weekday',
    label: 'Weekday',
    column: 'Entry day',
    note: 'By the session date of the entry. A weekend or holiday session is listed under its own weekday.',
  },
  {
    key: 'hold_bucket',
    label: 'Holding time',
    column: 'Held',
    note: 'By the holding time of the trade; a partial exit counts its money in its own bucket. Overnight: closed in a later session than it opened.',
  },
]

/** Human label for a "How you trade" bucket key. */
export function bucketLabel(key: string): string {
  const labels: Record<string, string> = {
    other: 'Other (outside 09:15-15:30)',
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

export interface DataQualityState {
  key: 'collected' | 'awaiting' | 'open' | 'included' | 'not_placed'
  step: string
  label: string
  count: number
  detail?: string
  title?: string
}

export const IN_COMPLETED_TITLE =
  'Fills of completed trades. Partial exits of positions still open are counted under Attributed, position still open; their realised P&L is already in your totals.'
export const ATTRIBUTED_OPEN_TITLE =
  'Fills of positions still open. Partial exits among them contribute realised P&L, which is already in your totals.'

/** The four execution states (plus "not placed" when any) of the data-quality strip. */
export function dataQualityStates(dq: DataQuality): DataQualityState[] {
  const states: DataQualityState[] = [
    {
      key: 'collected',
      step: '1',
      label: 'Collected',
      count: dq.collected,
      detail: 'manual and unresolved fills',
    },
    {
      key: 'awaiting',
      step: '2',
      label: 'Awaiting attribution',
      count: dq.awaiting_attribution,
      detail:
        dq.awaiting_attribution > 0
          ? `${dq.awaiting_grace} in the safety window, ${dq.awaiting_other} other`
          : undefined,
    },
    {
      key: 'open',
      step: '3',
      label: 'Attributed, position still open',
      count: dq.attributed_open,
      detail: 'partial exits: realised P&L already in totals',
      title: ATTRIBUTED_OPEN_TITLE,
    },
    {
      key: 'included',
      step: '4',
      label: 'In completed trades',
      count: dq.included,
      title: IN_COMPLETED_TITLE,
    },
  ]
  if (dq.not_placed > 0) {
    states.push({
      key: 'not_placed',
      step: '5',
      label: 'Not placed',
      count: dq.not_placed,
      detail: 'opening not in the ledger or invalid',
    })
  }
  return states
}

/** One line on the open-position prices, or "prices not requested". */
export function dataQualityPrices(dq: DataQuality): string {
  const p = dq.prices
  if (!p.requested) return 'Prices not requested'
  if (p.open_positions === 0) return 'No open positions to price'
  const parts: string[] = []
  if (p.missing > 0) parts.push(`${p.missing} without a usable price`)
  if (p.stale > 0) parts.push(`${p.stale} stale, last close or cached`)
  return parts.length > 0 ? `Prices: ${parts.join(', ')}` : 'Prices: all live'
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

/** One line under the category selector: closed positions of the range per category. */
export function classificationLine(c: ClassificationBlock | null | undefined): string | null {
  if (!c) return null
  const k = c.closed
  return `Closed positions in this range: ${k.intraday} intraday, ${k.swing} swing, ${k.unclassified} unclassified.`
}

/** Unclassified positions count only in All trades; say so when a category is chosen. */
export function unclassifiedScopeNote(
  c: ClassificationBlock | null | undefined,
  category: DiscretionaryCategory
): string | null {
  if (!c || c.closed.unclassified <= 0) return null
  return category === 'all'
    ? `${c.closed.unclassified} unclassified position(s) are included in All trades only.`
    : `${c.closed.unclassified} unclassified position(s) are left out of this view; they appear in All trades only.`
}

/** Text of the one-line warning about fills left out of the completed-trade statistics. */
export function unresolvedWarningText(u: UnresolvedInRange | null | undefined): string | null {
  if (!u || !(u.total > 0)) return null
  const noun = u.total === 1 ? 'fill' : 'fills'
  return `${u.total} ${noun} in the selected range could not be attributed and ${u.total === 1 ? 'is' : 'are'} excluded from completed-trade statistics.`
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

/** Monthly or weekday rows as net P&L bars (positions with a net are counted in `count`). */
export function calendarBars(rows: CalendarRow[] | null | undefined): NetBar[] {
  return toBars(
    (rows ?? []).map((r) => ({
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
 * The caption that ties the curve's maximum drawdown to the Risk card, computed at run time from
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
  if (mode === 'day') {
    return {
      kind: risk ? 'day_curve' : 'day_no_risk',
      text: risk
        ? `This curve is end-of-day: one point per close date, so intra-day falls are not drawn and its maximum drawdown (${curveDd}) can be smaller than the per-position Risk card figure (${money(risk.max_drawdown)}).`
        : `This curve is end-of-day: one point per close date, so intra-day falls are not drawn. Maximum drawdown on this curve: ${curveDd}.`,
    }
  }
  if (!risk) {
    return {
      kind: 'no_risk',
      text: `Maximum drawdown on this curve: ${curveDd} (net, per closed position by close time, peak starts at 0). The server sent no Risk card to compare with.`,
    }
  }
  if (tradesTotal > tradesShown) {
    return {
      kind: 'truncated',
      text: `Curve shows the newest ${tradesShown} of ${tradesTotal} closed positions and starts from 0 at the first of them (maximum drawdown ${curveDd}); the Risk card covers all ${tradesTotal} (maximum drawdown ${money(risk.max_drawdown)}).`,
    }
  }
  const same =
    Math.abs(curve.maxDrawdown - risk.max_drawdown) <= DD_TOLERANCE &&
    Math.abs(curve.currentDrawdown - risk.current_drawdown) <= DD_TOLERANCE &&
    curve.skipped === risk.net_unavailable_positions
  if (same) {
    const left =
      curve.skipped > 0
        ? ` ${curve.skipped} position(s) without a net P&L are left out of both.`
        : ''
    return {
      kind: 'match',
      text: `Max drawdown on this curve equals the Risk card (${curveDd}).${left}`,
    }
  }
  return {
    kind: 'mismatch',
    text: `Not reconciled: the curve uses ${curve.used} position(s) with a net P&L (${curve.skipped} left out) and gives a maximum drawdown of ${curveDd}; the Risk card uses ${risk.positions} (${risk.net_unavailable_positions} left out) and gives ${money(risk.max_drawdown)}. Both are net, by close time, peak from 0; the position sets differ.`,
  }
}

// ---------------------------------------------------------------- section visibility by data

export type ProfileName = 'session' | 'short' | 'medium' | 'long'

export interface PeriodProfile {
  profile: ProfileName
  showMonthly: boolean
  showWeekday: boolean
  showByPeriodTabs: boolean
  /** Which tabs of "P&L by period" are offered, in order. */
  byPeriodTabs: ('daily' | 'weekly' | 'monthly')[]
  showDailyBars: boolean
  showBreakdownTables: boolean
  /** Secondary breakdowns sit under a "More breakdowns" toggle. */
  collapseSecondary: boolean
  /** The curve is one point per closed position (otherwise end-of-day). */
  perPositionCurve: boolean
  /** One visible line naming what is hidden or collapsed and why; null when nothing is. */
  note: string | null
}

/**
 * Which sections make sense for the data on screen. `closeDates` = distinct dates with a closed
 * position; `months` = number of calendar months in the monthly rows. A 1-day period is always
 * the session profile.
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
      showMonthly: false,
      showWeekday: false,
      showByPeriodTabs: false,
      byPeriodTabs: [],
      showDailyBars: false,
      showBreakdownTables: true,
      collapseSecondary: true,
      perPositionCurve: true,
      note: 'Month, weekday and period tables are hidden because this view covers one trading day. Symbol, side and product breakdowns are under More breakdowns.',
    }
  }
  if (closeDates > 31 || months >= 3) {
    return {
      profile: 'long',
      showMonthly: true,
      showWeekday: true,
      showByPeriodTabs: true,
      byPeriodTabs: ['daily', 'weekly', 'monthly'],
      showDailyBars: true,
      showBreakdownTables: true,
      collapseSecondary: false,
      perPositionCurve: false,
      note: null,
    }
  }
  if (closeDates <= 7) {
    return {
      profile: 'short',
      showMonthly: false,
      showWeekday: true,
      showByPeriodTabs: true,
      byPeriodTabs: ['daily'],
      showDailyBars: true,
      showBreakdownTables: true,
      collapseSecondary: true,
      perPositionCurve: true,
      note: `The monthly table and the weekly and monthly period tabs are hidden because the range has only ${closeDates} trading days. Symbol, side and product breakdowns are under More breakdowns.`,
    }
  }
  const monthly = months >= 2
  return {
    profile: 'medium',
    showMonthly: monthly,
    showWeekday: true,
    showByPeriodTabs: true,
    byPeriodTabs: monthly ? ['daily', 'weekly', 'monthly'] : ['daily', 'weekly'],
    showDailyBars: true,
    showBreakdownTables: true,
    collapseSecondary: true,
    perPositionCurve: true,
    note: monthly
      ? 'Symbol, side and product breakdowns are under More breakdowns.'
      : 'The monthly table and tab are hidden because the range sits in one calendar month. Symbol, side and product breakdowns are under More breakdowns.',
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

// ---------------------------------------------------------------- summary header text

export interface Verdict {
  tone: 'profit' | 'loss' | 'flat' | 'none'
  text: string
}

/** One plain sentence: profitable or not on net, the amount, the closed count and the win rate. */
export function verdictLine(
  summary: Pick<
    DiscretionarySummary,
    'realized_net_of_charges' | 'metrics' | 'charges_unavailable_trades'
  >,
  formatCurrency: (v: number) => string
): Verdict {
  const n = summary.metrics.trades_count
  if (n === 0) return { tone: 'none', text: 'No closed position in this range yet.' }
  const net = cleanMoney(summary.realized_net_of_charges)
  const rate =
    summary.metrics.win_rate == null ? '' : ` Win rate ${fmtWinRate(summary.metrics.win_rate)}.`
  const incomplete =
    summary.charges_unavailable_trades > 0
      ? ' Some charges are not estimated, so net is not complete.'
      : ''
  const count = `${n} closed position${n === 1 ? '' : 's'}`
  if (net > 0)
    return {
      tone: 'profit',
      text: `Profitable on net: ${formatCurrency(net)} after estimated charges from ${count}.${rate}${incomplete}`,
    }
  if (net < 0)
    return {
      tone: 'loss',
      text: `Not profitable on net: ${formatCurrency(Math.abs(net))} lost after estimated charges over ${count}.${rate}${incomplete}`,
    }
  return {
    tone: 'flat',
    text: `Break-even on net after estimated charges over ${count}.${rate}${incomplete}`,
  }
}

/**
 * The single biggest weakness, taken only from fields the server already sends, in this order:
 * charges turning a gross profit into a net loss; charges taking 30% or more of gross profit;
 * the largest loss bigger than the largest win; a current drawdown; a small sample. Null when
 * none applies. A description, not advice.
 */
export function weaknessLine(
  summary: Pick<
    DiscretionarySummary,
    | 'realized_gross'
    | 'realized_net_of_charges'
    | 'metrics'
    | 'consistency'
    | 'costs'
    | 'risk'
    | 'sample'
  >,
  formatCurrency: (v: number) => string
): string | null {
  if (summary.metrics.trades_count === 0) return null
  const net = cleanMoney(summary.realized_net_of_charges)
  if (summary.realized_gross > 0 && net < 0) {
    return `Charges turned a gross profit of ${formatCurrency(summary.realized_gross)} into a net loss.`
  }
  const pct = summary.costs?.charges_pct_of_gross_profit
  if (pct != null && pct >= 30) {
    return `Charges take ${pct.toFixed(1)}% of gross profit.`
  }
  const win = summary.consistency?.largest_win
  const loss = summary.consistency?.largest_loss
  if (win && loss) {
    const w = win.net_pnl ?? win.realized_pnl
    const l = Math.abs(loss.net_pnl ?? loss.realized_pnl)
    if (l > w) {
      return `Largest loss (${formatCurrency(l)}) is bigger than the largest win (${formatCurrency(w)}).`
    }
  } else if (loss && !win) {
    return `Largest loss is ${formatCurrency(Math.abs(loss.net_pnl ?? loss.realized_pnl))} and there is no winning position.`
  }
  const cur = summary.risk?.current_drawdown ?? 0
  if (cur > 0) {
    return `Currently ${formatCurrency(cur)} below the best net equity so far.`
  }
  if (summary.sample && !summary.sample.sufficient) {
    return `Low sample: ${summary.sample.completed_trades} completed trade(s) over ${summary.sample.trading_days} trading day(s), so ratios are indicative only.`
  }
  return null
}
