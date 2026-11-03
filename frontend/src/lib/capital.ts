/**
 * Capital entered on the Performance page, for the percent-of-capital view.
 *
 * It lives in the URL as `?capital=1000000` and is remembered per mode in
 * localStorage, so a Live capital is never shown in Sandbox. The server applies
 * the same rules (finite, above zero, at most MAX_CAPITAL).
 */

export type CapitalMode = 'live' | 'analyze'

/** 1 lakh crore rupees; the same ceiling the server enforces. */
export const MAX_CAPITAL = 1_000_000_000_000

/** The smallest capital the page accepts, in rupees. Keeps tiny values out of the URL. */
export const MIN_CAPITAL = 1

export const CAPITAL_TOO_SMALL_MESSAGE = 'Enter an amount of at least 1'
export const CAPITAL_INVALID_MESSAGE = 'Enter an amount greater than zero'

const PLAIN_NUMBER = /^\d+\.?\d*$|^\.\d+$/

/**
 * Reads a typed capital. Commas and spaces are dropped ("10,00,000" is 1000000).
 * A trailing point is fine ("1000." is 1000), so typing a decimal does not flag an error.
 * Returns null for blank text and for anything that is not a usable amount.
 */
export function parseCapital(raw: string | null): number | null {
  if (raw === null) return null
  const text = raw.replace(/[,\s]/g, '')
  if (!PLAIN_NUMBER.test(text)) return null
  const value = Number(text)
  if (!Number.isFinite(value) || value < MIN_CAPITAL || value > MAX_CAPITAL) return null
  return value
}

/** The inline message for typed text that cannot be used, or null for blank or usable text. */
export function capitalProblem(raw: string): string | null {
  if (raw.trim() === '' || parseCapital(raw) !== null) return null
  const text = raw.replace(/[,\s]/g, '')
  if (PLAIN_NUMBER.test(text)) {
    const value = Number(text)
    if (value > 0 && value < MIN_CAPITAL) return CAPITAL_TOO_SMALL_MESSAGE
  }
  return CAPITAL_INVALID_MESSAGE
}

export type CapitalPlan = {
  /** 'keep' leaves the URL alone; a string or null replaces its capital. */
  url: 'keep' | string | null
  /** A capital to remember for the mode, or null. */
  remember: number | null
}

/**
 * Decides what the URL capital becomes once the mode is known.
 * First load: a usable capital in the URL is kept and remembered; otherwise the
 * remembered one for the mode is used. A later mode switch never carries the
 * old capital over: it takes the new mode's remembered value, or none.
 */
export function planCapitalForMode(input: {
  first: boolean
  urlCapital: string | null
  stored: number | null
}): CapitalPlan {
  const { first, urlCapital, stored } = input
  const storedText = stored === null ? null : String(stored)
  if (!first) return { url: storedText, remember: null }
  const fromUrl = parseCapital(urlCapital)
  if (fromUrl !== null) return { url: 'keep', remember: fromUrl }
  return storedText === null ? { url: 'keep', remember: null } : { url: storedText, remember: null }
}

/** The localStorage key for one mode. */
export function capitalStorageKey(mode: CapitalMode): string {
  return `openalgo.performance.capital.${mode}`
}

/** The remembered capital for a mode, or null when none is stored or storage is unavailable. */
export function loadStoredCapital(mode: CapitalMode): number | null {
  try {
    return parseCapital(window.localStorage.getItem(capitalStorageKey(mode)))
  } catch {
    return null
  }
}

/** Remembers a capital for a mode. Storage may be unavailable; that is not an error. */
export function saveStoredCapital(mode: CapitalMode, capital: number): void {
  try {
    window.localStorage.setItem(capitalStorageKey(mode), String(capital))
  } catch {
    // Storage is blocked or full: the URL still holds the value.
  }
}

/** Forgets the remembered capital for a mode. */
export function clearStoredCapital(mode: CapitalMode): void {
  try {
    window.localStorage.removeItem(capitalStorageKey(mode))
  } catch {
    // Nothing to remove when storage is unavailable.
  }
}

/** Reference capital per strategy the recorded trades are taken as sized on (matches the server). */
export const REFERENCE_CAPITAL_PER_STRATEGY = 100_000

/** Rupee-valued fields. Ratios, counts, percentages, prices and quantities are never scaled. */
const RUPEE_FIELDS = new Set([
  'net_profit',
  'gross_profit',
  'gross_loss',
  'expectancy',
  'avg_win',
  'avg_loss',
  'max_drawdown',
  'ulcer_index',
  'best_day',
  'worst_day',
  'est_costs',
  'net_after_costs',
  'turnover',
  'avg_charge_per_trade',
  'net_expectancy',
  'net_pnl_std',
  'entry_slippage',
  'net_pnl',
  'avg_pnl',
  'gross',
  'realized_pnl',
  'cost',
  'mfe',
  'mae',
  'pnl',
  'value',
  // Insights panel
  'capital_base',
  'net',
  'mean_daily',
  'median_daily',
  'avg_winning_day',
  'avg_losing_day',
  'net_ex_best_day',
  'net_ex_best_five_days',
  'best_day_net',
  'net_ex_portfolio_best_day',
  'max_drawdown_trade_sequence',
  'avg_daily_peak_deployed',
  'max_deployed',
  'exp20',
  'adverse_total',
  'favourable_total',
  'portfolio_dd_with',
  'portfolio_dd_without',
  'drawdown_change',
])

/** Arrays of two rupee bounds. */
const RUPEE_RANGES = new Set([
  'expectancy_ci90',
  'expectancy_ci95',
  'bootstrap_ci90',
  'bootstrap_ci95',
])

/** Sub-trees that are not rupee data and are passed through untouched. */
const SKIPPED_KEYS = new Set(['capital_view', 'data_health'])

function scaleNode(node: unknown, factor: number): unknown {
  if (Array.isArray(node)) return node.map((item) => scaleNode(item, factor))
  if (node === null || typeof node !== 'object') return node
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    if (SKIPPED_KEYS.has(key)) out[key] = value
    else if (RUPEE_FIELDS.has(key) && typeof value === 'number') out[key] = value * factor
    else if (RUPEE_RANGES.has(key) && Array.isArray(value))
      out[key] = value.map((v) => (typeof v === 'number' ? v * factor : v))
    else out[key] = scaleNode(value, factor)
  }
  return out
}

/**
 * Display what-if: every rupee figure in a Performance response multiplied by
 * `factor` (capital per strategy / REFERENCE_CAPITAL_PER_STRATEGY). Returns a new
 * object; the input, the stored P&L and the CSV export are never changed. A factor
 * of 1, or a missing one, returns the input as it is.
 */
export function scaleRupeeFigures<T>(data: T, factor: number | null | undefined): T {
  if (data === undefined || data === null) return data
  if (factor === null || factor === undefined || !Number.isFinite(factor) || factor === 1) {
    return data
  }
  return scaleNode(data, factor) as T
}
