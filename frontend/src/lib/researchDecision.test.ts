import { describe, expect, it } from 'vitest'
import { decisionNote, researchDecision, STOPPED_TEXT } from './runSummary'

const open = { research_state: 'promising' as const, rejection_basis: null, revisit_when: null }
const rejected = {
  research_state: 'rejected' as const,
  rejection_basis: 'not-robust',
  revisit_when: 'Only a new hypothesis.',
}

describe('researchDecision', () => {
  it('is null while the strategy is still open or has no recorded state', () => {
    expect(researchDecision(open)).toBeNull()
    expect(researchDecision({ ...open, research_state: undefined })).toBeNull()
  })

  it('names a stopped decision and its recorded basis in plain words', () => {
    expect(researchDecision(rejected)).toEqual({
      label: 'Rejected (not robust)',
      revisit: 'Only a new hypothesis.',
    })
    expect(
      researchDecision({ ...rejected, research_state: 'shelved', rejection_basis: null })
    ).toEqual({ label: 'Shelved', revisit: 'Only a new hypothesis.' })
  })
})

describe('decisionNote', () => {
  it('says the next step follows the research decision when the run result differs from it', () => {
    for (const v of ['edge', 'marginal', 'inconclusive'] as const) {
      expect(decisionNote(rejected, v)).toContain('next step follows the research decision')
    }
  })

  it('stays silent when the two agree, when nothing is stopped, or when there is no result', () => {
    expect(decisionNote(rejected, 'no-edge')).toBeNull()
    expect(decisionNote(open, 'marginal')).toBeNull()
    expect(decisionNote(rejected, null)).toBeNull()
    expect(decisionNote(rejected, 'none')).toBeNull()
  })
})

it('uses one sentence for a stopped version', () => {
  expect(STOPPED_TEXT).toBe('Do not continue this version.')
})
