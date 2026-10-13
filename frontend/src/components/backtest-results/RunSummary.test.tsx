import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import {
  makeCard,
  makeRun,
  makeRunDetail,
  makeSwingDetail,
  makeSwingRun,
} from '@/test/backtestResultsFixtures'
import { EvaluationTable } from './EvaluationTable'
import { RunRobustness } from './RunRobustness'
import { RunSummary } from './RunSummary'

vi.mock('./LineChart', () => ({ LineChart: ({ title }: { title: string }) => <div>{title}</div> }))

const IDEA = makeCard({
  research_state: 'rejected',
  rejection_basis: 'not-robust',
  revisit_when: 'Only a new, pre-specified hypothesis, recorded as a separate idea.',
})

describe('RunSummary', () => {
  it('shows the three answers as separate, labelled results', () => {
    render(<RunSummary idea={IDEA} run={makeSwingRun()} detail={makeSwingDetail()} />)
    expect(within(screen.getByTestId('summary-verdict')).getByText('Marginal')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('summary-disposition')).getByText('Rejected (not-robust)')
    ).toBeInTheDocument()
    expect(screen.getByTestId('summary-promotion')).toHaveTextContent(/Not ready/i)
    expect(screen.getByTestId('summary-reconcile')).toHaveTextContent(/different questions/)
  })

  it('leads with the selection-edge conclusion and portfolio results', () => {
    render(<RunSummary idea={IDEA} run={makeSwingRun()} detail={makeSwingDetail()} />)
    expect(screen.getByTestId('summary-selection-note')).toHaveTextContent(
      /does not show that the selection rule caused it/
    )
    const p = screen.getByTestId('summary-portfolio')
    expect(p).toHaveTextContent('+10.6%')
    expect(p).toHaveTextContent('+161.6%')
    expect(p).toHaveTextContent('-23.0%')
    expect(p).toHaveTextContent('all 2396 sessions')
    expect(p).toHaveTextContent(/Annual volatility9.8%/)
    expect(screen.getByTestId('summary-portfolio-basis')).toHaveTextContent(
      /30 positions of Rs 1 lakh \(a Rs 30 lakh book, profits not reinvested\)/
    )
    expect(screen.getByTestId('portfolio-chart')).toHaveTextContent(
      'Drawdown (% below the prior peak)'
    )
  })

  it('keeps both benchmark findings and defines the per-trade figure', () => {
    render(<RunSummary idea={IDEA} run={makeSwingRun()} detail={makeSwingDetail()} />)
    expect(screen.getByTestId('summary-random-entry')).toHaveTextContent('difference -309 bps')
    expect(screen.getByTestId('summary-equal-weight')).toHaveTextContent(
      /\+14.6% against \+20.8% a year/
    )
    expect(screen.getByTestId('summary-equal-weight')).toHaveTextContent(
      /Supporting diagnostic: not a gate and not decisive.*Not reproducible/
    )
    expect(screen.getByTestId('summary-supporting-note')).toHaveTextContent(/not decisive/)
    expect(screen.getByTestId('summary-trades')).toHaveTextContent(
      /Mean net return per trade: \+16.10% of the position's notional/
    )
    expect(screen.getByTestId('summary-trades')).toHaveTextContent(/not a portfolio return/)
  })

  it('marks a corrected gate and recommends stopping', () => {
    render(<RunSummary idea={IDEA} run={makeSwingRun()} detail={makeSwingDetail()} />)
    expect(screen.getByTestId('summary-strengths')).toHaveTextContent(
      /G3.3 Walk-forward: 17 of 19.*the sealed record says fail/
    )
    expect(screen.getByTestId('summary-failed')).toHaveTextContent(/G3.2/)
    expect(screen.getByTestId('summary-corrections')).toHaveTextContent(
      /2 gates re-scored.*status changed for G3.3 only/
    )
    const action = screen.getByTestId('summary-action')
    expect(action).toHaveTextContent('Do not continue this version.')
    expect(action).toHaveTextContent(/Only if work resumes.*\(G0.1\)/)
  })

  it('says portfolio figures are unavailable instead of inventing them', () => {
    render(
      <RunSummary
        idea={makeCard()}
        run={makeRun()}
        detail={makeRunDetail({
          evaluations: { corrections: [], portfolio: null, diagnostics: [] },
        })}
      />
    )
    expect(screen.getByTestId('summary-portfolio-unavailable')).toHaveTextContent(/not available/)
    expect(screen.queryByTestId('portfolio-chart')).not.toBeInTheDocument()
    expect(screen.getByTestId('summary-benchmarks')).toHaveTextContent(/No benchmark/)
  })
})

describe('EvaluationTable with corrections', () => {
  it('shows the corrected reading beside the recorded row', () => {
    const run = makeSwingRun()
    render(
      <EvaluationTable
        evaluation={run.evaluation!}
        corrections={makeSwingDetail().evaluations?.corrections}
      />
    )
    const row = screen.getByTestId('gate-G3.3')
    expect(row).toHaveTextContent('0 of 19 windows')
    expect(within(row).getByTestId('corrected-G3.3')).toHaveTextContent('17 of 19 windows')
    expect(screen.getByTestId('evaluation-corrections-note')).toHaveTextContent(
      /sealed run's original record/
    )
  })
})

describe('RunRobustness cost basis', () => {
  it('labels the flat headline cost and adds the modelled cost row', () => {
    render(<RunRobustness run={makeSwingRun()} />)
    expect(screen.getByText('24 (engine flat cost, headline)')).toBeInTheDocument()
    expect(screen.getByTestId('cost-modelled-row')).toHaveTextContent('30.25')
    expect(screen.getByTestId('cost-modelled-row')).toHaveTextContent('1604.11')
    expect(screen.getByTestId('cost-basis-note')).toHaveTextContent(/G1.1 and G1.3 only/)
  })
})
