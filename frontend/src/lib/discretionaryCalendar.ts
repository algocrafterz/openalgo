import type { DiscretionarySummary, PeriodRow } from '@/api/discretionaryPnl'
import { cleanMoney, netIncomplete } from '@/lib/discretionaryPnlView'

/**
 * Month calendar of realised net P&L, built from the daily rows (`by_period.daily`) of one
 * existing summary request whose range is the month. Nothing is calculated here except sums and
 * counts of the daily values, so the month total is exactly the sum of the days shown.
 */

export type DayStatus = 'profit' | 'loss' | 'flat' | 'none' | 'unavailable' | 'untracked'

export interface CalendarDay {
  date: string
  day: number
  /** Net realised P&L after estimated charges; null for a day with no recorded close. */
  net: number | null
  /** Completed trades whose final close is on this day (wins and losses of those). */
  trades: number
  wins: number
  losses: number
  /**
   * profit / loss / flat: the day has realised P&L. none: no trade was recorded. unavailable:
   * no trade is shown, but the broker trades of the day were not collected, so it may not be a
   * day without trades. untracked: before the start of the tracked history (nothing is known).
   */
  status: DayStatus
  /** Realised P&L is shown but part of it is missing (a charge estimate or a collection run). */
  incomplete: boolean
  /** Plain-language reason for incomplete / unavailable; null otherwise. */
  note: string | null
  future: boolean
}

export interface MonthTotals {
  /** Sum of the daily net values shown in the month. */
  net: number
  tradingDays: number
  profitableDays: number
  losingDays: number
  flatDays: number
  /** Days whose figures are incomplete or unavailable. */
  incompleteDays: number
  trades: number
  wins: number
  losses: number
}

export interface MonthCalendar {
  month: string
  /** Weeks, Monday first; null pads the days outside the month. */
  weeks: (CalendarDay | null)[][]
  days: CalendarDay[]
  totals: MonthTotals
}

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/

export function monthKeyOf(date: string): string {
  return date.slice(0, 7)
}

export function isMonthKey(value: string): boolean {
  return MONTH_RE.test(value)
}

export function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split('-').map(Number)
  const index = y * 12 + (m - 1) + delta
  const year = Math.floor(index / 12)
  const mon = (index % 12) + 1
  return `${year}-${String(mon).padStart(2, '0')}`
}

function daysInMonth(month: string): number {
  const [y, m] = month.split('-').map(Number)
  return new Date(Date.UTC(y, m, 0)).getUTCDate()
}

/** "November 2026". */
export function monthTitle(month: string): string {
  const [y, m] = month.split('-').map(Number)
  return new Intl.DateTimeFormat('en-US', {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(y, m - 1, 1)))
}

/**
 * The range requested for a month: the first day to the last day, or to today for the current
 * month (the server rejects a range that starts in the future and clips one that ends there).
 */
export function monthRange(month: string, today: string): { start: string; end: string } {
  const last = `${month}-${String(daysInMonth(month)).padStart(2, '0')}`
  return { start: `${month}-01`, end: last < today ? last : today }
}

export function canGoNext(month: string, today: string): boolean {
  return month < monthKeyOf(today)
}

/** Earlier months are reachable until the month the tracked history starts in. */
export function canGoPrevious(month: string, reliableSince: string | undefined): boolean {
  return !reliableSince || month > monthKeyOf(reliableSince)
}

function dayNote(parts: string[]): string | null {
  return parts.length > 0 ? parts.join('; ') : null
}

function buildDay(
  date: string,
  row: PeriodRow | undefined,
  ctx: { today: string; unsynced: Set<string>; historyStart: string | undefined }
): CalendarDay {
  const future = date > ctx.today
  const beforeHistory = !!ctx.historyStart && date < ctx.historyStart
  const notCollected = ctx.unsynced.has(date)
  const day = Number(date.slice(8, 10))
  if (!row) {
    if (future) {
      return {
        date,
        day,
        net: null,
        trades: 0,
        wins: 0,
        losses: 0,
        status: 'none',
        incomplete: false,
        note: null,
        future,
      }
    }
    if (beforeHistory) {
      return {
        date,
        day,
        net: null,
        trades: 0,
        wins: 0,
        losses: 0,
        status: 'untracked',
        incomplete: false,
        note: 'Before the start of the tracked history.',
        future,
      }
    }
    const note = notCollected
      ? 'Your broker trades were not collected on this day, so trades may be missing.'
      : null
    return {
      date,
      day,
      net: null,
      trades: 0,
      wins: 0,
      losses: 0,
      status: note ? 'unavailable' : 'none',
      incomplete: !!note,
      note,
      future,
    }
  }
  const net = cleanMoney(row.net_of_charges)
  const reasons: string[] = []
  if (netIncomplete(row)) {
    reasons.push('some trades have no charge estimate, so net P&L is not complete')
  }
  if (notCollected) reasons.push('broker trades were not collected on this day')
  const note = dayNote(reasons)
  return {
    date,
    day,
    net,
    trades: row.trades,
    wins: row.wins,
    losses: row.losses,
    status: net > 0 ? 'profit' : net < 0 ? 'loss' : 'flat',
    incomplete: note !== null,
    note: note ? note.charAt(0).toUpperCase() + note.slice(1) : null,
    future,
  }
}

/**
 * Builds the month grid from the summary of the range of that month. A day with a daily row is a
 * trading day (its realised money counts on the day of the close); a day without one is a day
 * with no trades, unless the server says its data is missing.
 */
export function buildMonthCalendar(
  summary: Pick<DiscretionarySummary, 'by_period' | 'sync_health' | 'period'>,
  month: string,
  today: string
): MonthCalendar {
  const rows = new Map<string, PeriodRow>()
  for (const r of summary.by_period?.daily ?? []) {
    if (monthKeyOf(r.period) === month) rows.set(r.period, r)
  }
  const unsynced = new Set(summary.sync_health?.days_without_sync ?? [])
  // The server starts the range no earlier than its tracked history.
  const first = `${month}-01`
  const historyStart = summary.period?.start > first ? summary.period.start : undefined
  const total = daysInMonth(month)
  const days: CalendarDay[] = []
  for (let d = 1; d <= total; d++) {
    const date = `${month}-${String(d).padStart(2, '0')}`
    days.push(buildDay(date, rows.get(date), { today, unsynced, historyStart }))
  }

  const traded = days.filter((d) => d.net !== null)
  const totals: MonthTotals = {
    net: cleanMoney(Math.round(traded.reduce((s, d) => s + (d.net ?? 0), 0) * 100) / 100),
    tradingDays: traded.length,
    profitableDays: traded.filter((d) => d.status === 'profit').length,
    losingDays: traded.filter((d) => d.status === 'loss').length,
    flatDays: traded.filter((d) => d.status === 'flat').length,
    incompleteDays: days.filter((d) => d.incomplete).length,
    trades: traded.reduce((s, d) => s + d.trades, 0),
    wins: traded.reduce((s, d) => s + d.wins, 0),
    losses: traded.reduce((s, d) => s + d.losses, 0),
  }

  const [y, m] = month.split('-').map(Number)
  const lead = (new Date(Date.UTC(y, m - 1, 1)).getUTCDay() + 6) % 7 // Monday = 0
  const cells: (CalendarDay | null)[] = [...Array(lead).fill(null), ...days]
  while (cells.length % 7 !== 0) cells.push(null)
  const weeks: (CalendarDay | null)[][] = []
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))
  return { month, weeks, days, totals }
}
