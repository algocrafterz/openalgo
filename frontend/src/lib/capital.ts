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
