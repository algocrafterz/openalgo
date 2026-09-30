import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { makeRun } from '@/test/backtestResultsFixtures'
import { DataCoverage } from './DataCoverage'

const FRESH = { years: 10, data_age_days: 0 }

describe('DataCoverage', () => {
  it('states how far back the run tested, on what data, and how old that data is', () => {
    render(
      <DataCoverage
        run={makeRun({ ...FRESH, period: { start: '2016-10-03', end: '2026-09-25' } })}
      />
    )
    const box = screen.getByTestId('data-coverage')
    expect(within(box).getByText(/2016-10-03 to 2026-09-25/)).toBeInTheDocument()
    expect(within(box).getByText(/10\.0 years/)).toBeInTheDocument()
    expect(within(box).getByText(/800 trading days/)).toBeInTheDocument()
    expect(within(box).getByText(/2026-09-29/)).toBeInTheDocument()
    expect(within(box).getByText(/same day/)).toBeInTheDocument()
    expect(within(box).getByText('5m')).toBeInTheDocument()
    expect(within(box).getByText('fno-208')).toBeInTheDocument()
    expect(within(box).getByText(/9\.4 bps round trip/)).toBeInTheDocument()
  })

  it('shows no caution for a long, fresh run', () => {
    render(<DataCoverage run={makeRun(FRESH)} />)
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it('warns when the history is short', () => {
    render(<DataCoverage run={makeRun({ years: 2.4, data_age_days: 0 })} />)
    expect(screen.getByRole('note')).toHaveTextContent(/2\.4 years/)
  })

  it('warns when the data snapshot is stale and says how old it is', () => {
    render(<DataCoverage run={makeRun({ years: 10, data_age_days: 30 })} />)
    expect(screen.getByRole('note')).toHaveTextContent(/30 days old/)
    expect(screen.getByTestId('data-coverage')).toHaveTextContent('30 days old')
  })

  it('renders n/a and no caution when coverage cannot be measured', () => {
    render(<DataCoverage run={makeRun({ years: null, data_age_days: null })} />)
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(screen.getByTestId('data-coverage')).toHaveTextContent('n/a')
  })

  it('omits the trading-day count when the run did not record it', () => {
    const run = makeRun(FRESH)
    render(<DataCoverage run={{ ...run, metrics: { ...run.metrics, days: undefined } }} />)
    expect(screen.getByTestId('data-coverage')).not.toHaveTextContent('trading days')
  })
})
