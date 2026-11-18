import type {
  BreakdownRow,
  DiscretionaryPosition,
  DiscretionarySummary,
  DiscretionaryTrade,
  PriceStatus,
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
}

/** Plain text for a raw reason or attribution code; unknown codes are shown with spaces. */
export function reasonText(code: string | null | undefined): string {
  if (!code) return '—'
  return REASON_TEXT[code] ?? code.replace(/_/g, ' ')
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
