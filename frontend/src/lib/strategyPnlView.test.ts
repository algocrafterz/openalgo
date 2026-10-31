import { describe, expect, it } from 'vitest'
import { edgeShortLabel, fmtDuration, fmtR } from './strategyPnlView'

describe('fmtDuration', () => {
  it('shows an em dash when unknown', () => {
    expect(fmtDuration(null)).toBe('—')
    expect(fmtDuration(undefined)).toBe('—')
  })
  it('formats minutes, hours and days', () => {
    expect(fmtDuration(0)).toBe('0m')
    expect(fmtDuration(42)).toBe('42m')
    expect(fmtDuration(59.6)).toBe('1h 00m')
    expect(fmtDuration(125)).toBe('2h 05m')
    expect(fmtDuration(1439)).toBe('23h 59m')
    expect(fmtDuration(1440 + 180)).toBe('1d 3h')
  })
})

describe('edge labels and fmtR', () => {
  it('maps verdicts to short labels', () => {
    expect(edgeShortLabel('likely real')).toBe('Likely real')
    expect(edgeShortLabel('likely negative')).toBe('Likely negative')
    expect(edgeShortLabel('not distinguishable from zero')).toBe('Unclear')
    expect(edgeShortLabel(undefined)).toBe('Too few trades')
  })
  it('formats R with a sign', () => {
    expect(fmtR(1.5)).toBe('+1.50R')
    expect(fmtR(-0.25)).toBe('-0.25R')
    expect(fmtR(null)).toBe('—')
  })
})
