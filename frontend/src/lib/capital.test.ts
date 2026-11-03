import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  CAPITAL_INVALID_MESSAGE,
  CAPITAL_TOO_SMALL_MESSAGE,
  capitalProblem,
  capitalStorageKey,
  clearStoredCapital,
  loadStoredCapital,
  MAX_CAPITAL,
  MIN_CAPITAL,
  parseCapital,
  planCapitalForMode,
  saveStoredCapital,
} from './capital'

describe('parseCapital', () => {
  it.each([
    ['1000000', 1000000],
    ['1,000,000', 1000000],
    ['10,00,000', 1000000],
    [' 10 00 000 ', 1000000],
    ['250000.5', 250000.5],
    ['1.5', 1.5],
    ['1000.', 1000],
    ['1', 1],
    ['1.0', 1],
    ['999999999999.99', 999999999999.99],
    ['1,000.', 1000],
    [String(MAX_CAPITAL), MAX_CAPITAL],
  ])('accepts %j', (raw, expected) => {
    expect(parseCapital(raw)).toBe(expected)
  })

  it.each([
    [null],
    [''],
    ['   '],
    ['0'],
    ['0.0'],
    ['.0000001'],
    ['0.5'],
    ['.5'],
    ['0.999'],
    ['-5'],
    ['abc'],
    ['.'],
    ['1..2'],
    ['1.2.3'],
    ['nan'],
    ['NaN'],
    ['inf'],
    ['Infinity'],
    ['1e5'],
    ['1e400'],
    ['12abc'],
    ['0x10'],
    ['+5'],
    ['1.2.3'],
    [String(MAX_CAPITAL + 1)],
  ])('rejects %j', (raw) => {
    expect(parseCapital(raw)).toBeNull()
  })
})

describe('exponent-free values', () => {
  it('has a minimum of one rupee', () => {
    expect(MIN_CAPITAL).toBe(1)
  })

  it.each([
    [String(MAX_CAPITAL)],
    ['999999999999.99'],
    ['1'],
    ['250000.5'],
  ])('stringifies %j without an exponent', (raw) => {
    const parsed = parseCapital(raw)
    expect(parsed).not.toBeNull()
    expect(String(parsed)).not.toContain('e')
  })
})

describe('capitalProblem', () => {
  it('names the minimum for amounts below one', () => {
    expect(capitalProblem('0.5')).toBe(CAPITAL_TOO_SMALL_MESSAGE)
    expect(capitalProblem('.0000001')).toBe(CAPITAL_TOO_SMALL_MESSAGE)
  })
  it('uses the general message for other unusable text', () => {
    expect(capitalProblem('abc')).toBe(CAPITAL_INVALID_MESSAGE)
    expect(capitalProblem('0')).toBe(CAPITAL_INVALID_MESSAGE)
    expect(capitalProblem('-5')).toBe(CAPITAL_INVALID_MESSAGE)
  })
  it('has no message for blank or usable text', () => {
    expect(capitalProblem('')).toBeNull()
    expect(capitalProblem('  ')).toBeNull()
    expect(capitalProblem('1,000')).toBeNull()
  })
})

describe('planCapitalForMode', () => {
  it('keeps and remembers a usable URL capital on first load', () => {
    expect(planCapitalForMode({ first: true, urlCapital: '500000', stored: 100 })).toEqual({
      url: 'keep',
      remember: 500000,
    })
  })
  it('seeds from storage on first load when the URL has none', () => {
    expect(planCapitalForMode({ first: true, urlCapital: null, stored: 250000 })).toEqual({
      url: '250000',
      remember: null,
    })
  })
  it('leaves the URL alone on first load with nothing stored', () => {
    expect(planCapitalForMode({ first: true, urlCapital: null, stored: null })).toEqual({
      url: 'keep',
      remember: null,
    })
  })
  it('never carries capital across a mode switch', () => {
    expect(planCapitalForMode({ first: false, urlCapital: '500000', stored: null })).toEqual({
      url: null,
      remember: null,
    })
    expect(planCapitalForMode({ first: false, urlCapital: '500000', stored: 100000 })).toEqual({
      url: '100000',
      remember: null,
    })
  })
})

describe('storage', () => {
  afterEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('uses a different key for each mode', () => {
    expect(capitalStorageKey('live')).not.toBe(capitalStorageKey('analyze'))
  })

  it('keeps Live and Sandbox capital apart', () => {
    saveStoredCapital('live', 500000)
    expect(loadStoredCapital('live')).toBe(500000)
    expect(loadStoredCapital('analyze')).toBeNull()
    saveStoredCapital('analyze', 100000)
    expect(loadStoredCapital('live')).toBe(500000)
    clearStoredCapital('live')
    expect(loadStoredCapital('live')).toBeNull()
    expect(loadStoredCapital('analyze')).toBe(100000)
  })

  it('ignores a stored value that is no longer valid', () => {
    window.localStorage.setItem(capitalStorageKey('live'), 'garbage')
    expect(loadStoredCapital('live')).toBeNull()
  })

  it('tolerates storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(loadStoredCapital('live')).toBeNull()
    expect(() => saveStoredCapital('live', 1)).not.toThrow()
    expect(() => clearStoredCapital('live')).not.toThrow()
  })
})
