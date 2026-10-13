import { describe, expect, it } from 'vitest'
import { makeCard, makeSwingDetail, makeSwingRun } from '@/test/backtestResultsFixtures'
import { buildRunSummary, effectiveRows, shortEvidence } from './runSummary'

const IDEA = makeCard({
  id: 'IDEA-028',
  research_state: 'rejected',
  rejection_basis: 'not-robust',
  revisit_when: 'Only a new, pre-specified hypothesis, recorded as a separate idea.',
})

describe('effectiveRows', () => {
  it('applies a correction and keeps the recorded row attached', () => {
    const run = makeSwingRun()
    const rows = effectiveRows(run.evaluation, makeSwingDetail().evaluations?.corrections)
    const g33 = rows.find((r) => r.gate === 'G3.3')
    expect(g33?.status).toBe('pass')
    expect(g33?.correction?.recorded.status).toBe('fail')
    expect(rows.find((r) => r.gate === 'G3.4')?.correction).toBeUndefined()
  })
})

describe('shortEvidence', () => {
  it('keeps the first clause and truncates long text', () => {
    expect(shortEvidence('a b; c d')).toBe('a b')
    expect(shortEvidence('x'.repeat(200), 20)).toHaveLength(20)
  })
})

describe('buildRunSummary', () => {
  const summary = buildRunSummary(IDEA, makeSwingRun(), makeSwingDetail())

  it('keeps the three answers apart and reconciles them', () => {
    expect(summary.verdict).toBe('MARGINAL')
    expect(summary.researchState).toBe('rejected')
    expect(summary.promotion).toBe('NOT_READY')
    expect(summary.reconcile).toMatch(/different questions/)
  })

  it('explains that profit did not establish a selection edge', () => {
    expect(summary.selectionNote).toMatch(/random entry into the same stocks/)
    expect(summary.selectionNote).toMatch(/-309 bps/)
    expect(summary.selectionNote).toMatch(
      /out-of-sample result fell short of the criterion \(G3.2\)/
    )
    expect(summary.selectionNote).not.toMatch(/equal-weight/)
  })

  it('states the equal-weight comparison as supporting, with its reproducibility', () => {
    expect(summary.supportingNote).toMatch(/^Supporting diagnostic, not decisive/)
    expect(summary.supportingNote).toMatch(/20.8% against 14.6% a year/)
    expect(summary.supportingNote).toMatch(/same return per unit of volatility \(1.07\)/)
    expect(summary.supportingNote).toMatch(/not reproducible: a historical diagnostic/)
    const kept = makeSwingDetail()
    const diag = kept.evaluations?.diagnostics[0]
    if (diag) diag.reproducible = true
    const withScript = buildRunSummary(IDEA, makeSwingRun(), kept)
    expect(withScript.supportingNote).toMatch(/reproducible from its kept script/)
    expect(withScript.limitations.join(' ')).not.toMatch(/cannot be reproduced/)
  })

  it('reads failures and strengths from the corrected rows', () => {
    expect(summary.failed.map((f) => f.gate)).toEqual(['G3.2'])
    const g33 = summary.strengths.find((s) => s.gate === 'G3.3')
    expect(g33?.correctedFrom).toBe('fail')
    expect(summary.strengths.map((s) => s.gate)).toEqual(['G3.3', 'G3.4', 'G2.2'])
  })

  it('lists limitations a reader must weigh', () => {
    const text = summary.limitations.join(' ')
    expect(text).toMatch(/Hard gates not measured for this run: G4.1/)
    expect(text).toMatch(/survivorship bias/)
    expect(text).toMatch(/flat 24 bps/)
    expect(text).toMatch(/rebuilt after the run/)
    expect(text).toMatch(/cannot be reproduced; it is not used as decisive evidence/)
  })

  it("recommends stopping and keeps the platform's next gate secondary", () => {
    expect(summary.action.text).toBe('Do not continue this version.')
    expect(summary.action.why).toMatch(/separate idea/)
    expect(summary.action.platform?.gate).toBe('G0.1')
  })

  it('uses the platform next action for an active idea and cites only G3.2 without benchmarks', () => {
    const active = buildRunSummary(
      makeCard({ research_state: 'active' }),
      makeSwingRun({ baseline: undefined }),
      makeSwingDetail({ evaluations: { corrections: [], portfolio: null, diagnostics: [] } })
    )
    expect(active.action.text).toMatch(/one code path/)
    expect(active.action.platform).toBeNull()
    expect(active.reconcile).toBeNull()
    expect(active.selectionNote).toMatch(/G3.2/)
    expect(active.selectionNote).not.toMatch(/random entry/)
    expect(active.supportingNote).toBeNull()
    expect(active.failed.map((f) => f.gate)).toEqual(['G3.2', 'G3.3'])
  })
})
