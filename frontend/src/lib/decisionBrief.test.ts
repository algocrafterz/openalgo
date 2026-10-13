import { describe, expect, it } from 'vitest'
import type { GateRow } from '@/api/backtestResults'
import {
  makeCard,
  makeRun,
  makeRunDetail,
  makeSwingDetail,
  makeSwingRun,
} from '@/test/backtestResultsFixtures'
import { buildDecisionBrief } from './decisionBrief'

const REJECTED = makeCard({
  research_state: 'rejected',
  rejection_basis: 'not-robust',
  revisit_when: 'Only a new, pre-specified hypothesis, recorded as a separate idea.',
})

const GATE_ID = /\bG\d\.\d\b/

function row(gate: string, title: string, status: GateRow['status'], hard = false): GateRow {
  return { gate, title, status, hard, value: null, threshold: 't', evidence: 'e' }
}

/** Every string the brief shows on the first screen, links excluded. */
function visibleText(b: ReturnType<typeof buildDecisionBrief>): string {
  return [
    b.headline,
    b.reason,
    b.basis,
    b.supporting ?? '',
    ...b.caveats,
    ...b.findings.map((f) => f.text),
    ...b.figures.flatMap((f) => [f.label, f.value, f.hint]),
    b.action.text,
    b.action.why,
  ].join(' ')
}

describe('buildDecisionBrief', () => {
  it('leads a profitable but unproven run with one plain recommendation', () => {
    const b = buildDecisionBrief(REJECTED, makeSwingRun(), makeSwingDetail())
    expect(b.headline).toBe('Do not pursue this version.')
    expect(b.tone).toBe('stop')
    expect(b.reason).toBe(
      "The portfolio made money, but the tests did not show that the strategy's selection rule caused the profit."
    )
    expect(b.action).toEqual({
      text: 'Do not continue this version.',
      why: 'Only a new, pre-specified hypothesis, recorded as a separate idea.',
    })
  })

  it('shows portfolio figures with their period and basis, not per-trade averages', () => {
    const b = buildDecisionBrief(REJECTED, makeSwingRun(), makeSwingDetail())
    const byLabel = Object.fromEntries(b.figures.map((f) => [f.label, f]))
    expect(byLabel['Annual return'].value).toBe('+10.6%')
    expect(byLabel['Worst fall (max drawdown)'].value).toBe('-23.0%')
    expect(byLabel.Trades.value).toBe('301')
    expect(byLabel['Test period'].value).toBe('2021-01-01 to 2026-09-25')
    expect(byLabel['Test period'].hint).toMatch(/^portfolio measured 2017-02-01 to /)
    expect(byLabel['Average per trade']).toBeUndefined()
    expect(b.basis).toMatch(/30 positions of Rs 1 lakh each/)
    expect(b.basis).toMatch(/after the engine's flat 24 bps round-trip cost/)
  })

  it('puts the random-entry control and the out-of-sample failure first, in plain words', () => {
    const b = buildDecisionBrief(REJECTED, makeSwingRun(), makeSwingDetail())
    expect(b.findings.length).toBeLessThanOrEqual(3)
    expect(b.findings[0].text).toMatch(/^Random entries did better/)
    expect(b.findings[0].text).toMatch(/\+17.76% per trade, against \+14.67% for the strategy/)
    expect(b.findings[0].text).toMatch(/on the 299 trades that could be matched/)
    expect(b.findings[0].href).toBe('#random-entry')
    expect(b.findings[1].text).toMatch(/^It failed the test on unseen data/)
    expect(b.findings[1].href).toBe('#gate-G3.2')
    expect(visibleText(b)).not.toMatch(GATE_ID)
  })

  it('keeps the equal-weight comparison a qualified, secondary note', () => {
    const b = buildDecisionBrief(REJECTED, makeSwingRun(), makeSwingDetail())
    expect(b.supporting).toMatch(/^Supporting check, not decisive: on a different, equal-weight/)
    expect(b.supporting).toMatch(/not comparable with the portfolio figures above/)
    expect(b.findings.some((f) => /equal-weight/.test(f.text))).toBe(false)
  })

  it('keeps decision-critical warnings without gate ids', () => {
    const b = buildDecisionBrief(REJECTED, makeSwingRun(), makeSwingDetail())
    expect(b.caveats).toContain(
      '1 required test was not run. A test that was not run is not a pass.'
    )
    expect(b.caveats.some((c) => /survivorship bias/.test(c))).toBe(true)
  })

  it('reports a losing run without a portfolio as unavailable, never zero', () => {
    const run = makeRun({ evaluation: undefined, baseline: undefined })
    const b = buildDecisionBrief(
      makeCard({ research_state: 'needs_more_evidence' }),
      run,
      makeRunDetail()
    )
    expect(b.headline).toBe('Not yet: more evidence is needed.')
    expect(b.reason).toBe('The strategy lost money after trading costs.')
    const byLabel = Object.fromEntries(b.figures.map((f) => [f.label, f]))
    expect(byLabel['Annual return'].value).toBe('Not available')
    expect(byLabel['Worst fall (max drawdown)'].value).toBe('Not available')
    expect(byLabel['Average per trade'].value).toBe('-0.01%')
    expect(byLabel['Average per trade'].hint).toMatch(/not a portfolio return/)
    expect(b.basis).toMatch(/^No whole-portfolio return was recorded/)
    expect(b.findings[0].text).toMatch(/^It lost money after costs/)
  })

  it('caps findings at three and counts the failed tests left for the details', () => {
    const run = makeRun({
      metrics: { ...makeRun().metrics, gross_bps: -4, net_bps: -16 },
      baseline: {
        gross_bps: -1,
        net_bps: -12.6,
        trade_net_bps: -15.8,
        excess_net_bps: -3.2,
        trades: 2000,
        sampled: true,
      },
      evaluation: {
        version: 1,
        dimensions: [
          {
            name: 'all',
            rows: [
              row('G1.2', 'Net expectancy at 1.0x cost', 'fail', true),
              row('G3.2', 'Out-of-sample result', 'fail'),
              row('G3.3', 'Walk-forward', 'fail'),
              row('G4.5', 'Dispersion', 'fail', true),
              row('G6.4', 'Tick size', 'fail', true),
            ],
          },
        ],
        summary: { counts: { fail: 5 }, hard_failed: ['G1.2', 'G4.5', 'G6.4'] },
      },
    })
    const b = buildDecisionBrief(REJECTED, run, makeRunDetail())
    expect(b.reason).toBe('The strategy lost money, even before trading costs.')
    expect(b.findings.map((f) => f.href)).toEqual(['#gate-G1.2', '#random-entry', '#gate-G3.2'])
    expect(b.findings[0].text).toBe(
      'It lost money even before costs: -0.04% per trade before costs and -0.16% per trade on average after costs, over 1,200 trades.'
    )
    expect(b.findings[1].text).toMatch(/on a sample of 2,000 matched trades/)
    expect(b.moreFailed).toBe(3)
  })

  it('says when a lead over random entry is not statistically clear', () => {
    const run = makeRun({
      metrics: { ...makeRun().metrics, net_bps: 20 },
      baseline: {
        gross_bps: 30,
        net_bps: 15,
        trade_net_bps: 20,
        excess_net_bps: 5,
        excess_t_stat: 0.8,
        trades: 100,
      },
    })
    const b = buildDecisionBrief(makeCard({ research_state: 'promising' }), run, makeRunDetail())
    expect(b.findings[0].text).toMatch(/^It beat random entries.*not statistically clear\.$/)
    expect(b.reason).toBe('The strategy made money and no test failed.')
  })

  it('uses the recorded next action when research has not stopped', () => {
    const detail = makeRunDetail({
      next_action: {
        action: 'Run the parameter sweep',
        gate: 'G4.1',
        reason: 'Settings must be robust',
      },
    })
    const b = buildDecisionBrief(makeCard({ research_state: 'active' }), makeRun(), detail)
    expect(b.action).toEqual({ text: 'Run the parameter sweep', why: 'Settings must be robust' })
  })
})
