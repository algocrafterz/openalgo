import { describe, expect, it } from 'vitest'
import { getTerm } from '@/lib/backtestGlossary'

describe('engine 1.6 glossary entries', () => {
  it.each([
    ['session_coverage', 'missing session'],
    ['common_session_basis', 'common-session'],
    ['ambiguous_exit', 'stop and the target'],
    ['data_gap_in_trade', 'missing'],
  ])('%s has a short and a long definition', (id, word) => {
    const e = getTerm(id)
    expect(e?.short.length).toBeGreaterThan(20)
    expect(`${e?.short} ${e?.long}`.toLowerCase()).toContain(word.toLowerCase())
    expect(`${e?.short} ${e?.long}`).not.toMatch(/\b(arm|armed|paper trading|virtual trading)\b/i)
  })
})
