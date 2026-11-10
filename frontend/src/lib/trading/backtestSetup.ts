/**
 * The facts a backtest run rests on, written for a trader who is about to press Run.
 *
 * **Nothing here is a setting and nothing here is a default of its own.** Every value is read
 * from where the run reads it: the chart (the instrument and the timeframe), the script's own
 * `strategy()` declaration (capital, order size, commission, slippage, fills), or what the
 * trader typed (the dates and the script's inputs). The language decides which of those a host
 * may change (see `backtestInputs.ts`); this only says, for each, whose it is, so that nobody
 * has to guess what a figure on the report rests on.
 */

import type { DeclaredSettings } from './backtestInputs'
import { backtestLookbackDays } from './intervals'

/** Whose a fact is: the chart's, the script's, or the trader's. */
export type SetupSource = 'chart' | 'script' | 'you'

export const SOURCE_LABEL: Record<SetupSource, string> = {
  chart: 'from the chart',
  script: 'set in the script',
  you: 'set by you',
}

export interface SetupFact {
  key: string
  label: string
  value: string
  source: SetupSource
  /** One line under the value, when the value alone would mislead. */
  hint?: string
}

/**
 * An engine identifier as words: `perTrade` is "per trade", `next_open` is "next open". The
 * words are the script's own; this only breaks the identifier up, so a value the engine adds
 * later still reads sensibly and nothing here has to know the full set.
 */
export function plain(identifier: string): string {
  return identifier
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim()
    .toLowerCase()
}

/** A declaration the program has not been read for yet (the script did not compile). */
export const NOT_READ = 'Not read yet'

export interface ChartTarget {
  symbol: string
  exchange: string
  interval: string
}

/** The instrument and the timeframe: the chart's, and never this panel's to change. */
export function chartFacts(target: ChartTarget | null): SetupFact[] {
  return [
    {
      key: 'instrument',
      label: 'Instrument',
      value: target
        ? `${target.symbol}${target.exchange ? ` (${target.exchange})` : ''}`
        : 'No chart open',
      source: 'chart',
    },
    {
      key: 'timeframe',
      label: 'Timeframe',
      value: target ? target.interval : 'No chart open',
      source: 'chart',
    },
  ]
}

/**
 * Capital, order size, commission and slippage, as the script declares them.
 *
 * `sizing` is the order size the run will send when it differs from the declared one, which is
 * the case when the script takes its size from one of its own inputs; `sizeIsYours` says so.
 */
export function declaredFacts(
  declared: DeclaredSettings | null,
  sizing: number | null,
  sizeIsYours: boolean
): SetupFact[] {
  if (!declared) {
    return [
      { key: 'capital', label: 'Starting capital', value: NOT_READ, source: 'script' },
      { key: 'size', label: 'Position size', value: NOT_READ, source: 'script' },
      { key: 'commission', label: 'Commission', value: NOT_READ, source: 'script' },
      { key: 'slippage', label: 'Slippage', value: NOT_READ, source: 'script' },
    ]
  }
  const units = sizing === null ? declared.qty : sizing
  return [
    {
      key: 'capital',
      label: 'Starting capital',
      value: `${declared.capital.toLocaleString()} ${declared.currency}`.trim(),
      source: 'script',
    },
    {
      key: 'size',
      label: 'Position size',
      value: `${units} ${plain(declared.qtyType)}`.trim(),
      source: sizeIsYours ? 'you' : 'script',
      hint: sizeIsYours ? "Taken from one of the script's inputs below." : undefined,
    },
    {
      key: 'commission',
      label: 'Commission',
      value: `${declared.commission} ${plain(declared.commissionType)}`.trim(),
      source: 'script',
    },
    {
      key: 'slippage',
      label: 'Slippage',
      value: `${declared.slippage} ticks`,
      source: 'script',
    },
  ]
}

/**
 * What the costs on this run do and do not include.
 *
 * A strategy that declares neither commission nor slippage is tested for free, and on Indian
 * equity the statutory charges alone (STT, exchange fees, GST, stamp duty) are a real share of
 * a short-horizon edge. This panel adds no charge of its own, so that has to be said.
 */
export function costsNote(declared: DeclaredSettings | null): string | null {
  if (!declared) return null
  const own = `This panel adds no charges of its own: STT, exchange fees, GST and stamp duty are in the figures only if the script's commission covers them.`
  if (declared.commission === 0 && declared.slippage === 0) {
    return `The script declares no commission and no slippage, so these figures are before trading costs. ${own}`
  }
  return own
}

/** A lookback in days as a trader would say it. */
export function lookbackLabel(days: number): string {
  if (days === 60) return '2 months'
  if (days % 365 === 0) {
    const years = days / 365
    return `${years} year${years === 1 ? '' : 's'}`
  }
  return `${days} days`
}

/**
 * Where the date range came from. The default follows the timeframe (a range that is right for
 * one interval is wrong for another) until the trader sets dates, after which it stays theirs.
 */
export function rangeNote(interval: string, datesAreTheirs: boolean): string {
  if (datesAreTheirs)
    return 'Dates set by you. They stay as you left them when the timeframe changes.'
  if (!interval) return ''
  return `Default for ${interval}: the last ${lookbackLabel(backtestLookbackDays(interval))}. It follows the timeframe until you set dates.`
}

/** The rest of what the script declares about how it trades: shown, not offered. */
export function advancedFacts(declared: DeclaredSettings): { label: string; value: string }[] {
  return [
    { label: 'Fills on', value: plain(declared.fillOn) || '-' },
    { label: 'Pyramiding', value: String(declared.pyramiding) },
    { label: 'Product', value: declared.product || '-' },
    { label: 'Closes at session end', value: declared.closeOnSessionEnd ? 'Yes' : 'No' },
  ]
}
