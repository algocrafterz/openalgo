import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { makeCard, makeLatest } from '@/test/backtestResultsFixtures'
import { StrategyCatalogue } from './StrategyCatalogue'

function renderRow(card: ReturnType<typeof makeCard>) {
  render(
    <MemoryRouter>
      <StrategyCatalogue ideas={[card]} />
    </MemoryRouter>
  )
  return screen.getByTestId(`catalogue-row-${card.id}`)
}

describe('StrategyCatalogue', () => {
  it('shows the readable columns for a tested strategy', () => {
    const row = renderRow(
      makeCard({
        id: 'IDEA-010',
        title: 'Gamma fade',
        summary: 'Fade the first hour move.',
        classification: {
          strategy_category: 'Mean Reversion',
          strategy_tags: [],
          trading_style: 'intraday',
          direction: null,
          timeframe: '5m',
          market: null,
        },
        latest_run: makeLatest({
          date: '2026-10-01',
          run_verdict: 'NO_EDGE',
          metrics: { trades: 500, gross_bps: 12, net_bps: 4.5, profit_factor: 1.2 },
          hard_failed: ['G2.2', 'G4.5'],
          rupee_pnl: { net_rs: -16400.18, total_cost_rs: 164978.74, basis: 'modelled' },
          next_action: { action: 'Compute the day-clustered t-statistic', reason: '', gate: null },
          book: {
            source: 'recorded',
            total_return_pct: 42.5,
            annual_return_pct: 6,
            max_drawdown_pct: -18.2,
            years: 7,
          },
        }),
      })
    )
    expect(within(row).getByText('Gamma fade')).toBeInTheDocument()
    // The compact row carries no hypothesis text; it is on the report.
    expect(within(row).queryByTestId('hypothesis')).toBeNull()
    expect(within(row).getByTestId('catalogue-run-meta')).toHaveTextContent('Last run 2026-10-01')
    // Style and timeframe are small text under the name, not a column of their own.
    expect(within(row).getByText(/IDEA-010 \/ intraday \/ 5m/)).toBeInTheDocument()
    expect(within(row).getByText('500')).toBeInTheDocument()
    expect(within(row).getByText('-Rs 16,400')).toBeInTheDocument()
    expect(within(row).getByText('-Rs 16,400')).toHaveClass('text-red-600')
    expect(within(row).getByText('+42.5% whole portfolio')).toBeInTheDocument()
    expect(within(row).getByText('1.20')).toBeInTheDocument()
    expect(within(row).getByText('-18.2%')).toBeInTheDocument()
    expect(within(row).getByTestId('plain-verdict-chip')).toHaveTextContent('Weak')
    expect(within(row).getByTestId('conclusion-sentence')).toHaveTextContent(
      /No reliable profit after costs.*2 required tests failed/
    )
    expect(within(row).getByTestId('next-action')).toHaveTextContent(
      'Compute the day-clustered t-statistic.'
    )
  })

  it('collapses ideas with no run into one line, and keeps tested ones in the table', () => {
    render(
      <MemoryRouter>
        <StrategyCatalogue
          ideas={[
            makeCard({ id: 'IDEA-1', title: 'Tested', latest_run: makeLatest() }),
            makeCard({ id: 'IDEA-2', title: 'Idea two', latest_run: null }),
            makeCard({ id: 'IDEA-3', title: 'Idea three', latest_run: null }),
          ]}
        />
      </MemoryRouter>
    )
    const untested = screen.getByTestId('untested-ideas')
    expect(untested).toHaveTextContent('2 not tested yet')
    expect(untested).not.toHaveAttribute('open')
    expect(within(untested).getByTestId('catalogue-row-IDEA-2')).toBeInTheDocument()
    expect(within(screen.getByTestId('strategy-catalogue')).getAllByRole('table')).toHaveLength(2)
    expect(within(untested).queryByTestId('catalogue-row-IDEA-1')).toBeNull()
  })

  it('shows a coverage chip only when the data has gaps', () => {
    const complete = renderRow(
      makeCard({
        id: 'A',
        latest_run: makeLatest({ session_coverage: { coverage_class: 'COMPLETE' } as never }),
      })
    )
    expect(within(complete).queryByTestId('catalogue-coverage')).toBeNull()
  })

  it('shows the coverage chip for a data set with material gaps', () => {
    const row = renderRow(
      makeCard({
        id: 'B',
        latest_run: makeLatest({
          session_coverage: { coverage_class: 'MATERIAL_GAPS', universe: {} } as never,
        }),
      })
    )
    expect(within(row).getByTestId('catalogue-coverage')).toBeInTheDocument()
  })

  it('says Not available instead of inventing a portfolio return', () => {
    const row = renderRow(
      makeCard({
        latest_run: makeLatest({ metrics: { trades: 500, gross_bps: 12, net_bps: 4.5 } }),
      })
    )
    expect(within(row).getAllByText('Not available').length).toBeGreaterThanOrEqual(3)
    expect(within(row).getByText('+4.5 bps per trade')).toBeInTheDocument()
  })

  it('falls back to the drawdown in R when the run has no portfolio book, and says so', () => {
    const row = renderRow(
      makeCard({
        latest_run: makeLatest({
          metrics: { trades: 500, gross_bps: 12, net_bps: 4.5, max_dd_r: 53.6 },
        }),
      })
    )
    expect(within(row).getByText('53.6R')).toBeInTheDocument()
    expect(within(row).getByText('in R, no book')).toBeInTheDocument()
  })

  it('shows an untested strategy without figures', () => {
    const row = renderRow(
      makeCard({ verdict: 'none', run_count: 0, latest_run: null, status: 'inbox' })
    )
    expect(within(row).getByText('No backtest recorded yet')).toBeInTheDocument()
    expect(within(row).getByTestId('plain-verdict-chip')).toHaveTextContent('Not tested')
    expect(within(row).getByTestId('next-action')).toHaveTextContent('Run a first backtest.')
  })

  it('tells the trader to stop on a rejected idea', () => {
    const row = renderRow(makeCard({ research_state: 'rejected' }))
    expect(within(row).getByTestId('next-action')).toHaveTextContent(
      'Do not continue this version.'
    )
  })

  it('keeps statistical columns out of the catalogue', () => {
    renderRow(makeCard())
    expect(screen.queryByText('t vs bar')).toBeNull()
    expect(screen.queryByText('vs random entry')).toBeNull()
    expect(screen.queryByText('OOS net bps')).toBeNull()
  })
})

describe('StrategyCatalogue research decision', () => {
  it('says beside the assessment when the researcher has stopped work on the strategy', () => {
    const row = renderRow(
      makeCard({
        id: 'IDEA-028',
        verdict: 'marginal',
        research_state: 'rejected',
        rejection_basis: 'not-robust',
        latest_run: makeLatest(),
      })
    )
    expect(within(row).getByTestId('plain-verdict-chip')).toHaveTextContent('Borderline')
    expect(within(row).getByTestId('research-decision-note')).toHaveTextContent(
      'Research decision: Rejected (not robust)'
    )
  })

  it('shows no decision note while the strategy is still open', () => {
    const row = renderRow(
      makeCard({ id: 'IDEA-030', research_state: 'promising', latest_run: makeLatest() })
    )
    expect(within(row).queryByTestId('research-decision-note')).toBeNull()
  })
})
