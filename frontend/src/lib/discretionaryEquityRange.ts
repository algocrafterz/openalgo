import type { PeriodRow } from '@/api/discretionaryPnl'
import type { CurvePoint } from '@/lib/discretionaryPnlView'

/**
 * Range choices of the Equity Curve card, and the calendar layout of the daily curve. Dates are
 * plain YYYY-MM-DD text; nothing here reads a clock or the server. The ranges are requests for
 * the existing summary call (period=custom, start, end): no new data is created.
 */

export type EquityRangeKey = 'period' | '1m' | '3m' | '6m' | 'all'

export interface EquityRangeOption {
  key: EquityRangeKey
  label: string
  /** Range to request; null for the report period, which the page already has. */
  range: { start: string; end: string } | null
  /** False when the tracked history is shorter than the range. */
  available: boolean
  /** Plain-language reason a choice is off; null when it is on. */
  reason: string | null
}

const MONTHS: Record<Exclude<EquityRangeKey, 'period' | 'all'>, { n: number; label: string }> = {
  '1m': { n: 1, label: '1M' },
  '3m': { n: 3, label: '3M' },
  '6m': { n: 6, label: '6M' },
}

function parts(date: string): [number, number, number] {
  const [y, m, d] = date.split('-').map(Number)
  return [y, m, d]
}

/** The same day of the month `months` earlier (clamped to the end of a shorter month). */
export function monthsBefore(date: string, months: number): string {
  const [y, m, d] = parts(date)
  const index = y * 12 + (m - 1) - months
  const year = Math.floor(index / 12)
  const month = (index % 12) + 1
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate()
  return `${year}-${String(month).padStart(2, '0')}-${String(Math.min(d, last)).padStart(2, '0')}`
}

/**
 * The choices of the card. `since` is the first tracked date the server reports
 * (`period.reliable_since`); a fixed range that would start before it cannot be shown in full,
 * so it is offered switched off with the reason. `all` starts at `since`.
 */
export function equityRangeOptions(input: { today: string; since: string }): EquityRangeOption[] {
  const { today, since } = input
  const out: EquityRangeOption[] = [
    { key: 'period', label: 'Report period', range: null, available: true, reason: null },
  ]
  for (const key of ['1m', '3m', '6m'] as const) {
    const { n, label } = MONTHS[key]
    const start = monthsBefore(today, n)
    const available = start >= since
    out.push({
      key,
      label,
      range: { start, end: today },
      available,
      reason: available
        ? null
        : `Your tracked history starts on ${since}, which is less than ${n} month${n === 1 ? '' : 's'} ago.`,
    })
  }
  out.push({
    key: 'all',
    label: 'All',
    range: { start: since, end: today },
    available: since <= today,
    reason: since <= today ? null : 'Nothing is tracked yet.',
  })
  return out
}

export function dayTime(date: string): number {
  const [y, m, d] = parts(date)
  return Date.UTC(y, m - 1, d) / 1000
}

function isWeekday(date: string): boolean {
  const day = new Date(`${date}T00:00:00Z`).getUTCDay()
  return day >= 1 && day <= 5
}

function nextDay(date: string): string {
  const [y, m, d] = parts(date)
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10)
}

export interface DaySlot<T> {
  date: string
  /** The row or point of this day; null when the day has none. */
  item: T | null
  /** Broker trades were not collected on this day. */
  notCollected: boolean
}

/**
 * One slot per calendar position from the first to the last day with a figure: every day that
 * has one, plus every weekday without one (no trades, a holiday, or trades not collected). A
 * weekend without a figure is left out, so a Friday and the next Monday sit side by side. A slot
 * without an item is a gap: no value, never zero.
 */
export function daySlots<T>(
  items: T[],
  dateOf: (item: T) => string,
  notCollected: readonly string[] = []
): DaySlot<T>[] {
  if (items.length === 0) return []
  const byDate = new Map(items.map((i) => [dateOf(i), i]))
  const missed = new Set(notCollected)
  const dates = [...byDate.keys()].sort()
  const first = dates[0]
  const last = dates[dates.length - 1]
  const out: DaySlot<T>[] = []
  for (let day = first; day <= last; day = nextDay(day)) {
    const item = byDate.get(day) ?? null
    if (item || isWeekday(day)) out.push({ date: day, item, notCollected: missed.has(day) })
  }
  return out
}

export function equityTimeline(
  points: CurvePoint[],
  notCollected: readonly string[] = []
): DaySlot<CurvePoint>[] {
  return daySlots(points, (p) => p.closed_at, notCollected)
}

export function dailyTimeline(
  rows: PeriodRow[],
  notCollected: readonly string[] = []
): DaySlot<PeriodRow>[] {
  return daySlots(rows, (r) => r.period, notCollected)
}

/** Days the server reports as not collected, inside the shown span. */
export function uncollectedInSpan(
  days: readonly string[] | undefined,
  start: string,
  end: string
): string[] {
  return (days ?? []).filter((d) => d >= start && d <= end).sort()
}

const SHORT = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })

export function shortDate(date: string): string {
  return SHORT.format(new Date(`${date}T00:00:00Z`))
}

/**
 * Plain-language flag for days whose broker trades were not collected. A day without a row has
 * no point (it may have had trades); a day with a row may be missing some of its trades.
 */
export function uncollectedDaysText(
  uncollected: readonly string[],
  datesWithFigure: ReadonlySet<string>
): string | null {
  if (uncollected.length === 0) return null
  const none = uncollected.filter((d) => !datesWithFigure.has(d))
  const some = uncollected.filter((d) => datesWithFigure.has(d))
  const list = (days: string[]) =>
    days.length > 6
      ? `${days.slice(0, 6).map(shortDate).join(', ')} and ${days.length - 6} more`
      : days.map(shortDate).join(', ')
  const parts: string[] = []
  if (none.length > 0) {
    parts.push(
      `Your broker trades were not collected on ${list(none)}. Those days have no point, and they may have had trades.`
    )
  }
  if (some.length > 0) {
    parts.push(
      `Your broker trades were not collected on ${list(some)}. Their figures are marked as incomplete.`
    )
  }
  return parts.join(' ')
}
