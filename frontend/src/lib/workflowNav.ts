/**
 * Day selection, polling and URL sync for the Daily Workflow page (pure; the page only renders).
 *
 * The `?day=` parameter is the source of truth for a selected day. Rules:
 *  - the latest day is never kept in the URL: selecting it clears `day`, and the page then follows
 *    the server's latest day ("live" mode);
 *  - only live mode (no selection, or a selection equal to the latest listed day) polls;
 *  - a historical selection is never polled, so it cannot change under the reader.
 */

export const POLL_MS = 60_000
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/

/** A usable `?day=` value, or undefined. */
export function parseDayParam(value: string | null | undefined): string | undefined {
  return value && DAY_RE.test(value) ? value : undefined
}

/** True when nothing is selected or the selection is the newest listed day. */
export function isLive(selected: string | undefined, days: string[] | undefined): boolean {
  if (selected === undefined) return true
  return Boolean(days && days.length > 0 && days[0] === selected)
}

/** react-query `refetchInterval` for the day query. Unknown day list: poll only without a selection. */
export function pollInterval(
  selected: string | undefined,
  days: string[] | undefined
): number | false {
  return isLive(selected, days) ? POLL_MS : false
}

/** The `day` query value after picking `picked`: null (clear the parameter) for the latest day. */
export function nextDayParam(picked: string, days: string[]): string | null {
  return days.length > 0 && days[0] === picked ? null : picked
}

const pad = (n: number) => String(n).padStart(2, '0')

/** Client-clock HH:MM:SS of a millisecond timestamp (local time of this browser). */
export function formatClock(ms: number): string {
  const d = new Date(ms)
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** \"Last updated 09:31:07\", or null before the first successful fetch (updatedAt 0 or missing). */
export function lastUpdatedText(updatedAt: number | undefined): string | null {
  return updatedAt ? `Last updated ${formatClock(updatedAt)}` : null
}
