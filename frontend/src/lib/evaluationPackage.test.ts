import { describe, expect, it } from 'vitest'
import type { EvaluationPackage } from '@/api/strategyPnl'
import {
  ATTACH_NOTE,
  downloadFilename,
  EVIDENCE_LABEL_CLASS,
  EVIDENCE_LABELS,
  FINDING_KIND_BADGE,
  OBSERVATION_KIND_TEXT,
  problemsToLines,
  promptForUse,
  selectionSignature,
  TOO_LARGE_NOTE,
} from './evaluationPackage'

const base = { include: ['A', 'B'], exclude: [] as string[] }
const sig = (
  filter = base,
  period: Parameters<typeof selectionSignature>[1] = '30d',
  range: { start: string; end: string } | undefined = undefined,
  capital: number | null = null,
  mode: 'live' | 'analyze' = 'live'
) => selectionSignature(filter, period, range, capital, mode)

describe('selectionSignature', () => {
  it('is stable under include and exclude order', () => {
    expect(sig({ include: ['B', 'A'], exclude: ['Y', 'X'] })).toBe(
      sig({ include: ['A', 'B'], exclude: ['X', 'Y'] })
    )
  })

  it('changes with each part of the selection', () => {
    const start = sig()
    const variants = [
      sig({ include: ['A'], exclude: [] }),
      sig({ include: ['A', 'B'], exclude: ['C'] }),
      sig(base, '7d'),
      sig(base, 'custom', { start: '2026-01-01', end: '2026-01-31' }),
      sig(base, '30d', undefined, 1000),
      sig(base, '30d', undefined, null, 'analyze'),
      sig({ include: [], exclude: [], none: true } as typeof base),
    ]
    for (const v of variants) expect(v).not.toBe(start)
    expect(new Set(variants).size).toBe(variants.length)
  })

  it('changes with the custom range but ignores a range on a fixed period', () => {
    const a = sig(base, 'custom', { start: '2026-01-01', end: '2026-01-31' })
    const b = sig(base, 'custom', { start: '2026-01-01', end: '2026-02-01' })
    expect(a).not.toBe(b)
    expect(sig(base, '30d', { start: 'x', end: 'y' })).toBe(sig(base, '30d'))
  })
})

const pkg = (text_with_data: string | null): Pick<EvaluationPackage, 'prompt'> => ({
  prompt: { text: 'plain', text_with_data },
})

describe('promptForUse', () => {
  it('uses the prompt with data when asked and available', () => {
    expect(promptForUse(pkg('with data'), true)).toEqual({
      text: 'with data',
      includesData: true,
      note: null,
    })
  })

  it('falls back to the prompt without data and says so', () => {
    const choice = promptForUse(pkg(null), true)
    expect(choice.text).toBe('plain')
    expect(choice.includesData).toBe(false)
    expect(choice.note).toBe(TOO_LARGE_NOTE)
  })

  it('tells the trader to attach the file when data is not included', () => {
    const choice = promptForUse(pkg('with data'), false)
    expect(choice.text).toBe('plain')
    expect(choice.note).toBe(ATTACH_NOTE)
  })
})

describe('downloadFilename', () => {
  it('keeps the server name and cleans unsafe characters', () => {
    const f = (name: unknown) => downloadFilename({ export: { data_filename: name } } as never)
    expect(f('performance-evaluation-live.json')).toBe('performance-evaluation-live.json')
    expect(f('../a b/c.json')).toBe('.._a_b_c.json')
    expect(f('')).toBe('performance-evaluation.json')
    expect(f(undefined)).toBe('performance-evaluation.json')
  })
})

describe('problemsToLines', () => {
  it('caps at 20 lines', () => {
    const many = Array.from({ length: 35 }, (_, i) => ({ path: `p${i}`, message: 'bad' }))
    const lines = problemsToLines(many)
    expect(lines).toHaveLength(20)
    expect(lines[0]).toBe('p0: bad')
  })

  it('handles a missing list and a missing path', () => {
    expect(problemsToLines(undefined)).toEqual([])
    expect(problemsToLines([{ path: '', message: 'only text' }])).toEqual(['only text'])
  })
})

describe('mappings', () => {
  it('has exactly the four evidence labels', () => {
    expect([...EVIDENCE_LABELS].sort()).toEqual(
      [
        'insufficient data',
        'likely negative',
        'no distinguishable edge',
        'possible edge (screening only)',
      ].sort()
    )
    expect(Object.keys(EVIDENCE_LABEL_CLASS).sort()).toEqual([...EVIDENCE_LABELS].sort())
  })

  it('maps finding kinds to the three trader words', () => {
    expect(
      Object.fromEntries(Object.entries(FINDING_KIND_BADGE).map(([k, v]) => [k, v.text]))
    ).toEqual({
      math: 'Calculated',
      heuristic: 'Rule of thumb',
      not_available: 'Not available',
    })
  })

  it('maps observation kinds to trader words', () => {
    expect(OBSERVATION_KIND_TEXT).toEqual({
      observed: 'Observed',
      inference: 'Inference',
      hypothesis: 'Hypothesis to test',
    })
  })
})
