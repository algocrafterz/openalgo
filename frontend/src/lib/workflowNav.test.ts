import { describe, expect, it } from 'vitest'
import {
  formatClock,
  isLive,
  lastUpdatedText,
  nextDayParam,
  POLL_MS,
  parseDayParam,
  pollInterval,
} from './workflowNav'

const days = ['2026-11-16', '2026-11-13', '2026-11-12']

describe('parseDayParam', () => {
  it('accepts only YYYY-MM-DD', () => {
    expect(parseDayParam('2026-11-13')).toBe('2026-11-13')
    expect(parseDayParam('11-13')).toBeUndefined()
    expect(parseDayParam('2026-11-13; x')).toBeUndefined()
    expect(parseDayParam(null)).toBeUndefined()
  })
})

describe('polling', () => {
  it('polls with no selection', () => {
    expect(pollInterval(undefined, days)).toBe(POLL_MS)
    expect(pollInterval(undefined, undefined)).toBe(POLL_MS)
  })
  it('polls when the selected day is the latest', () => {
    expect(isLive('2026-11-16', days)).toBe(true)
    expect(pollInterval('2026-11-16', days)).toBe(POLL_MS)
  })
  it('does not poll a historical selection', () => {
    expect(pollInterval('2026-11-13', days)).toBe(false)
  })
  it('does not poll a selection while the day list is unknown', () => {
    expect(pollInterval('2026-11-13', undefined)).toBe(false)
    expect(pollInterval('2026-11-13', [])).toBe(false)
  })
})

describe('nextDayParam', () => {
  it('clears the parameter for the latest day and keeps others', () => {
    expect(nextDayParam('2026-11-16', days)).toBeNull()
    expect(nextDayParam('2026-11-13', days)).toBe('2026-11-13')
    expect(nextDayParam('2026-11-13', [])).toBe('2026-11-13')
  })
})

describe('last updated', () => {
  it('formats the client clock as HH:MM:SS', () => {
    const t = new Date(2026, 10, 15, 9, 5, 7).getTime()
    expect(formatClock(t)).toBe('09:05:07')
    expect(lastUpdatedText(t)).toBe('Last updated 09:05:07')
  })
  it('is null before the first successful fetch', () => {
    expect(lastUpdatedText(0)).toBeNull()
    expect(lastUpdatedText(undefined)).toBeNull()
  })
})
