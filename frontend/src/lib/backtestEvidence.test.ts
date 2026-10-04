import { describe, expect, it } from 'vitest'
import {
  COST_COMPONENT_KEYS,
  EXCLUSION_LABELS,
  exclusionLabel,
  sortedExclusions,
  tradeSubsetForGate,
} from './backtestEvidence'

describe('tradeSubsetForGate', () => {
  it.each([
    ['G3.1', 'oos'],
    ['G3.2', 'oos'],
    ['G6.5', 'gap_exits'],
    ['G1.2', 'all'],
    ['G1.4', 'all'],
    ['G2.1', 'all'],
  ])('maps %s to %s', (gate, subset) => {
    expect(tradeSubsetForGate(gate)).toBe(subset)
  })

  it.each([
    'G3.3',
    'G3.4',
    'G3.5',
    'G3',
    'G1',
    'G1.3',
    'G2.2',
    'G2.10',
    'G6.5.1',
    'G4.1',
    'G6.4',
    'X.2',
    '',
    null,
    undefined,
  ])('has no subset for %s', (gate) => {
    expect(tradeSubsetForGate(gate)).toBeNull()
  })
})

describe('exclusion labels', () => {
  it('labels every known code in plain language', () => {
    expect(exclusionLabel('MAX_TRADES_DAY')).toBe('Daily trade limit reached')
    expect(exclusionLabel('OPEN_WINDOW')).toBe('Inside the opening window')
    expect(exclusionLabel('ENTRY_CUTOFF')).toBe('After the entry cutoff')
    for (const code of Object.keys(EXCLUSION_LABELS)) {
      expect(EXCLUSION_LABELS[code].length).toBeGreaterThan(5)
    }
    expect(Object.keys(EXCLUSION_LABELS)).toHaveLength(19)
  })

  it('shows an unknown code as the raw code', () => {
    expect(exclusionLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW')
  })

  it('sorts by count descending, then code', () => {
    expect(sortedExclusions({ B: 2, A: 2, C: 9 }).map((e) => e.code)).toEqual(['C', 'A', 'B'])
    expect(sortedExclusions(undefined)).toEqual([])
  })
})

describe('COST_COMPONENT_KEYS', () => {
  it('lists the modelled cost keys only', () => {
    expect(COST_COMPONENT_KEYS).toContain('cost_stt_bps')
    expect(COST_COMPONENT_KEYS).toContain('modeled_cost_bps')
    expect(COST_COMPONENT_KEYS).not.toContain('gross_bps')
  })
})
