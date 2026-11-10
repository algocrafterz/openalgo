import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PortfolioView } from '@/api/backtestResults'
import { makeCard, makeRun } from '@/test/backtestResultsFixtures'
import { KeyMetrics, ReportHeader, ResultCard } from './ReportSummary'

const COST = {
  modelled_net_pnl_rs: -16400.18,
  modelled_total_cost_rs: 164978.74,
  pnl_basis: 'modelled: each trade sized on its own',
}
const BOOK: PortfolioView = {
  source: 'recorded',
  stats: { total_return_pct: 61.5, annual_return_pct: 8.2, max_drawdown_pct: -22.9, years: 7 },
}

describe('KeyMetrics', () => {
  it('shows net P&L first with the cost and gross beside it, from the recorded fields', () => {
    render(<KeyMetrics run={makeRun({ cost_model: COST })} portfolio={BOOK} />)
    const net = screen.getByTestId('kpi-net-pnl')
    expect(net).toHaveTextContent('-Rs 16,400')
    expect(net.querySelector('.text-red-600')).not.toBeNull()
    // gross is net plus the recorded total cost, shown as secondary text
    expect(net).toHaveTextContent('Costs Rs 1,64,979')
  })

  it('shows the six figures with the portfolio return, drawdown and its period', () => {
    render(<KeyMetrics run={makeRun({ cost_model: COST })} portfolio={BOOK} />)
    expect(screen.getByTestId('kpi-return')).toHaveTextContent('+61.5%')
    expect(screen.getByTestId('kpi-return')).toHaveTextContent('over 7.0 years, +8.2% a year')
    expect(screen.getByTestId('kpi-drawdown')).toHaveTextContent('-22.9%')
    expect(screen.getByTestId('kpi-profit-factor')).toHaveTextContent('0.95')
    expect(screen.getByTestId('kpi-trades')).toHaveTextContent('1200')
    expect(screen.getByTestId('kpi-avg-trade')).toHaveTextContent('bps')
  })

  it('reads n/a with the reason when the run has no rupee result or portfolio book', () => {
    render(<KeyMetrics run={makeRun()} portfolio={null} />)
    expect(screen.getByTestId('kpi-net-pnl')).toHaveTextContent('n/a')
    expect(screen.getByTestId('kpi-net-pnl')).toHaveTextContent('No rupee result recorded')
    expect(screen.getByTestId('kpi-return')).toHaveTextContent('n/a')
    expect(screen.getByTestId('kpi-return')).toHaveTextContent('No portfolio book')
    // drawdown falls back to the run's own R figure and says so
    expect(screen.getByTestId('kpi-drawdown')).toHaveTextContent('12.5R')
    expect(screen.getByTestId('kpi-drawdown')).toHaveTextContent('In R: no portfolio book')
  })

  it('treats an unavailable book as no book', () => {
    render(
      <KeyMetrics run={makeRun()} portfolio={{ source: 'recorded', unavailable: 'no slots' }} />
    )
    expect(screen.getByTestId('kpi-return')).toHaveTextContent('n/a')
  })

  it('does not colour a missing or positive-neutral figure as a loss', () => {
    render(<KeyMetrics run={makeRun()} portfolio={null} />)
    expect(screen.getByTestId('kpi-net-pnl').querySelector('.text-red-600')).toBeNull()
    expect(screen.getByTestId('kpi-net-pnl').querySelector('.text-green-600')).toBeNull()
  })
})

describe('ReportHeader', () => {
  it('shows what the strategy is and what was tested, with no verdict', () => {
    render(<ReportHeader title="IDEA-1: Gamma fade" summary="Fade the open." run={makeRun()} />)
    const header = screen.getByTestId('report-header')
    expect(within(header).getByRole('heading')).toHaveTextContent('IDEA-1: Gamma fade')
    expect(header).toHaveTextContent('Fade the open.')
    expect(header).toHaveTextContent('fno-208')
    expect(header).toHaveTextContent('2021-01-01 to 2026-09-25')
    expect(header).toHaveTextContent('Last run 2026-09-30')
    expect(within(header).queryByTestId('plain-verdict-chip')).toBeNull()
  })
})

describe('ResultCard', () => {
  it('puts the verdict and its explanation first, then the six figures', () => {
    render(
      <ResultCard
        idea={makeCard()}
        run={makeRun({ cost_model: COST })}
        portfolio={BOOK}
        verdict="marginal"
      />
    )
    const card = screen.getByTestId('result-card')
    expect(within(card).getByTestId('plain-verdict-chip')).toHaveTextContent('Borderline')
    expect(within(card).getByTestId('plain-verdict-line')).toHaveTextContent(/Not convincing yet/i)
    const parts = ['plain-verdict-line', 'key-metrics'].map((id) => within(card).getByTestId(id))
    for (let i = 0; i < parts.length - 1; i++) {
      expect(
        parts[i].compareDocumentPosition(parts[i + 1]) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy()
    }
  })

  it('never shows a verdict when the run has none to show', () => {
    render(<ResultCard idea={makeCard()} run={makeRun()} verdict={null} />)
    expect(screen.queryByTestId('plain-verdict-chip')).toBeNull()
    expect(screen.getByTestId('no-run-verdict')).toBeInTheDocument()
    expect(screen.getByTestId('key-metrics')).toBeInTheDocument()
  })
})

describe('ResultCard research decision', () => {
  const rejected = makeCard({
    research_state: 'rejected',
    rejection_basis: 'not-robust',
    revisit_when: 'Only a new hypothesis.',
  })

  it('labels the calculated result and the research decision apart when they differ', () => {
    render(<ResultCard idea={rejected} run={makeRun()} verdict="marginal" />)
    const card = screen.getByTestId('result-card')
    expect(within(card).getByTestId('plain-verdict-chip')).toHaveTextContent('Borderline')
    expect(within(card).getByTestId('research-decision')).toHaveTextContent(
      'Research decision: Rejected (not robust)'
    )
    expect(within(card).getByTestId('decision-note')).toHaveTextContent(
      'next step follows the research decision'
    )
  })

  it('shows no note when the result already agrees with the decision', () => {
    render(<ResultCard idea={rejected} run={makeRun()} verdict="no-edge" />)
    expect(screen.getByTestId('research-decision')).toBeInTheDocument()
    expect(screen.queryByTestId('decision-note')).toBeNull()
  })

  it('shows nothing extra while the strategy is still open', () => {
    render(
      <ResultCard
        idea={makeCard({ research_state: 'promising' })}
        run={makeRun()}
        verdict="marginal"
      />
    )
    expect(screen.queryByTestId('research-decision')).toBeNull()
    expect(screen.queryByTestId('decision-note')).toBeNull()
  })
})
