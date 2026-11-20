import type {
  BreakdownRow,
  DataQuality,
  DiscretionaryPosition,
  DiscretionarySummary,
  DiscretionaryToday,
  DiscretionaryTrade,
  PendingAttribution,
  PeriodRow,
  PriceStatus,
  SampleContext,
  SyncHealth,
} from '@/api/discretionaryPnl'

/** The Performance page source: current-mode strategies (unchanged) or discretionary trading. */
export type PageSource = 'strategies' | 'discretionary'

export type SourceAvailability = 'pending' | 'enabled' | 'disabled'

/** URL value that selects the Discretionary source. Absent or anything else means strategies. */
export const DISCRETIONARY_SOURCE = 'discretionary'

/** Mirrors the Performance page's poll interval; quotes=1 polls at the same pace. */
export const DISCRETIONARY_POLL_MS = 60_000

export const EQUITY_CAPTION = 'Gross realized P&L, excludes charges and unrealized'
export const DRAWDOWN_BASIS = 'gross realized, excludes charges and unrealized'

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

/** Capital-derived figures the page never shows without a capital basis (ratio_capital is not shown at all). */
export const WITHHELD_LABELS: Record<string, string> = {
  sharpe_ratio: 'Sharpe',
  sortino_ratio: 'Sortino',
  return_pct: 'Return %',
}

export interface RatioCell {
  /** '--' when withheld. */
  value: string
  /** 'Not shown: no capital basis' when withheld. */
  hint?: string
  withheld: boolean
}

/** What to show for a ratio the backend may withhold: '--' plus the reason, never a made-up number. */
export function ratioCell(
  key: string,
  value: number | null | undefined,
  withheld: Record<string, string> | undefined,
  format: (v: number) => string = (v) => v.toFixed(2)
): RatioCell {
  const reason = withheld?.[key]
  if (reason !== undefined) return { value: '--', hint: `Not shown: ${reason}`, withheld: true }
  if (value == null || Number.isNaN(value)) return { value: '--', withheld: false }
  return { value: format(value), withheld: false }
}

/** One sentence naming every withheld metric and why, or '' when nothing is withheld. */
export function withheldSentence(withheld: Record<string, string> | undefined): string {
  const entries = Object.entries(withheld ?? {}).filter(([k]) => k in WITHHELD_LABELS)
  if (entries.length === 0) return ''
  const byReason = new Map<string, string[]>()
  for (const [key, reason] of entries) {
    byReason.set(reason, [...(byReason.get(reason) ?? []), WITHHELD_LABELS[key]])
  }
  return [...byReason.entries()]
    .map(([reason, names]) => `${names.join(', ')}: not shown: ${reason}`)
    .join('; ')
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

/** Backend warnings worth a banner: informational lines are left to the cards and the sync line. */
export function actionableWarnings(warnings: string[] | undefined): string[] {
  return (warnings ?? []).filter(
    (w) => !INFORMATIONAL_WARNING_PREFIXES.some((prefix) => w.startsWith(prefix))
  )
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
  return 'Net = gross realized minus estimated charges. Gross realized and the gross metrics are before charges.'
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
  return `No attributable trades available for performance analysis yet. ${collected} executions collected; ${unresolved} awaiting attribution. Their P&L is excluded until classification is resolved.`
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
      `${pending.other_today} execution(s) of today stay unresolved for another reason and are excluded from P&L; see Unresolved fills.`
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
      detail: 'manual and unresolved executions',
    },
    {
      key: 'awaiting',
      step: '2',
      label: 'Awaiting attribution',
      count: dq.awaiting_attribution,
      detail: dq.awaiting_attribution > 0 ? 'unresolved, excluded from P&L' : undefined,
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

export const PROVISIONAL_TITLE =
  'Pending attribution: provisional figures (not included in any total above)'
export const PROVISIONAL_EXPLANATION =
  'These executions are excluded from your P&L until classified as manual. Figures are computed from the broker fills as if they were yours; if any of them were placed by a strategy they would be removed.'

/** One card of the attribution split of the data-quality strip. */
export interface AttributionCard {
  key: 'manual' | 'unresolved' | 'strategy'
  label: string
  count: number
  detail: string
  /** Small secondary line (the safety-window split of the unresolved fills). */
  note?: string
}

/**
 * Who the executions of the period belong to: Manual (positively identified, in P&L),
 * Unresolved (awaiting attribution, excluded from P&L) and Strategy-attributed (excluded; from
 * `source_labels`, card omitted when the server does not send it). Unresolved fills are never
 * counted as manual: Manual is the collected fills minus the unresolved ones.
 */
export function attributionCards(
  dq: DataQuality,
  sourceLabels?: Record<string, number> | null
): AttributionCard[] {
  const manual = Math.max(0, dq.collected - dq.awaiting_attribution)
  const window = dq.grace_minutes ? `${dq.grace_minutes} minute safety window` : 'safety window'
  const cards: AttributionCard[] = [
    {
      key: 'manual',
      label: 'Manual',
      count: manual,
      detail:
        dq.not_placed > 0
          ? `positively identified, in P&L; ${dq.not_placed} could not be placed`
          : 'positively identified, in P&L',
    },
    {
      key: 'unresolved',
      label: 'Unresolved',
      count: dq.awaiting_attribution,
      detail: 'awaiting attribution, excluded from P&L',
      note:
        dq.awaiting_attribution > 0
          ? `${dq.awaiting_grace} inside the ${window}, ${dq.awaiting_other} past it`
          : undefined,
    },
  ]
  const strategy = sourceLabels?.SIGNAL_LIVE
  if (typeof strategy === 'number') {
    cards.push({
      key: 'strategy',
      label: 'Strategy-attributed',
      count: strategy,
      detail: 'strategy orders, excluded from this view',
    })
  }
  return cards
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
  if (!p) return iso ?? ''
  return `${p.d} ${MONTHS[p.m - 1]}${withYear ? ` ${p.y}` : ''}`
}

/** '13-19 Nov 2026', '28 Oct - 3 Nov 2026', '28 Dec 2025 - 3 Jan 2026' or '19 Nov 2026'. */
export function fmtDayRange(start: string, end: string, withYear = true): string {
  const a = parseIsoDay(start)
  const b = parseIsoDay(end)
  if (!a || !b) return start === end ? start : `${start} - ${end}`
  const year = withYear ? ` ${b.y}` : ''
  if (start === end) return `${b.d} ${MONTHS[b.m - 1]}${year}`
  if (a.y !== b.y) return `${fmtDayMonth(start, true)} - ${fmtDayMonth(end, true)}`
  if (a.m !== b.m) return `${a.d} ${MONTHS[a.m - 1]} - ${b.d} ${MONTHS[b.m - 1]}${year}`
  return `${a.d}-${b.d} ${MONTHS[b.m - 1]}${year}`
}

const FIXED_WINDOW_DAYS: Record<string, number> = { '7d': 7, '15d': 15, '30d': 30, '90d': 90 }
const PERIOD_NAMES: Record<string, string> = {
  '1d': 'Last session',
  '7d': 'Last 7 days',
  '15d': 'Last 15 days',
  '30d': 'Last 30 days',
  '90d': 'Last 90 days',
  ytd: 'Year to date',
  all: 'All time',
  custom: 'Custom range',
}

function isoMinusDays(iso: string, days: number): string | null {
  const p = parseIsoDay(iso)
  if (!p) return null
  const t = new Date(Date.UTC(p.y, p.m - 1, p.d - days))
  return t.toISOString().slice(0, 10)
}

/**
 * The selected period as asked for and as shown. The server clamps the start to the start of
 * ledger history (`reliable_since`), so a 7 or 30 day window can show fewer days; this says so.
 * `nominal` is the window as asked (null for 'all', which has no fixed start), `shown` the
 * served range, `note` the clamp sentence: only when the shown start is later than the nominal
 * start and equals `reliable_since`. `customStart` is the start the user picked (custom only).
 */
export function periodHeader(
  period: { period: string; start: string; end: string; reliable_since?: string },
  customStart?: string
): { name: string; nominal: string | null; shown: string; note: string | null } {
  const { start, end } = period
  const name = PERIOD_NAMES[period.period] ?? period.period
  const shown = fmtDayRange(start, end)
  let nominalStart: string | null = null
  const days = FIXED_WINDOW_DAYS[period.period]
  if (days) nominalStart = isoMinusDays(end, days - 1)
  else if (period.period === 'ytd') nominalStart = `${end.slice(0, 4)}-01-01`
  else if (period.period === 'custom') nominalStart = customStart ?? start
  else if (period.period === '1d') nominalStart = start
  const nominal = nominalStart ? fmtDayRange(nominalStart, end) : null
  const clamped =
    nominalStart !== null &&
    !!period.reliable_since &&
    start > nominalStart &&
    start === period.reliable_since
  const note = clamped
    ? `Showing ${fmtDayRange(start, end, false)}: ledger history starts ${fmtDayMonth(start)}.`
    : null
  return { name, nominal, shown, note }
}
