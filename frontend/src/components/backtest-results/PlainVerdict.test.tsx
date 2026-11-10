import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunVerdictValue, Verdict } from '@/api/backtestResults'
import { PLAIN_VERDICT, PlainVerdictLine, RUN_VERDICT_TO_VERDICT } from './plainVerdict'

const ALL: Verdict[] = ['edge', 'marginal', 'inconclusive', 'no-edge', 'none']

describe('plain verdict wording', () => {
  it.each(ALL)('%s has a label and a full sentence', (v) => {
    expect(PLAIN_VERDICT[v].label.length).toBeGreaterThan(2)
    expect(PLAIN_VERDICT[v].sentence.endsWith('.')).toBe(true)
  })

  it('avoids words the platform does not use and any status codes', () => {
    for (const v of ALL) {
      const text = `${PLAIN_VERDICT[v].label} ${PLAIN_VERDICT[v].sentence}`
      expect(text).not.toMatch(
        /\barm(ed|ing)?\b|paper trading|virtual trading|\bG\d\.\d\b|NO_EDGE/i
      )
    }
  })

  it('keeps inconclusive distinct from failed', () => {
    expect(PLAIN_VERDICT.inconclusive.sentence).toMatch(/does not mean the strategy failed/)
  })

  it('renders the chip and the sentence', () => {
    render(<PlainVerdictLine verdict="marginal" />)
    expect(screen.getByTestId('plain-verdict-chip')).toHaveTextContent('Borderline')
    expect(screen.getByTestId('plain-verdict-line')).toHaveTextContent(/not convincing yet/i)
  })
})

describe('server verdict to the label a trader reads', () => {
  const cases: [RunVerdictValue, string][] = [
    ['EDGE', 'Promising'],
    ['MARGINAL', 'Borderline'],
    ['INCONCLUSIVE', 'Needs more data'],
    ['NO_EDGE', 'Weak'],
  ]
  it.each(cases)('%s reads %s', (value, label) => {
    expect(PLAIN_VERDICT[RUN_VERDICT_TO_VERDICT[value]].label).toBe(label)
  })

  it('keeps the four labels distinct', () => {
    const labels = cases.map(([v]) => PLAIN_VERDICT[RUN_VERDICT_TO_VERDICT[v]].label)
    expect(new Set(labels).size).toBe(4)
  })
})
