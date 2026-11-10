import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { COVERAGE } from './fixtures/engineBlocks'
import { SessionCoverageTable } from './SessionCoverageTable'

describe('SessionCoverageTable', () => {
  it('lists symbols worst first with missing-session ranges, collapsed', () => {
    render(<SessionCoverageTable coverage={COVERAGE} />)
    const details = screen.getByTestId('session-coverage-details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    const rows = within(details).getAllByRole('row').slice(1)
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'coverage-row-BBB',
      'coverage-row-CCC',
      'coverage-row-AAA',
    ])
    const worst = screen.getByTestId('coverage-row-BBB')
    expect(worst).toHaveTextContent('2022-03-01 to 2022-03-14, 2023-05-02')
    expect(worst).toHaveTextContent('1360 of 1400')
    expect(worst).toHaveTextContent('55')
    expect(screen.getByTestId('coverage-row-CCC')).toHaveTextContent('not checked')
  })

  it('shows each symbol class, its expected sessions and its missing share', () => {
    render(<SessionCoverageTable coverage={COVERAGE} />)
    const bbb = screen.getByTestId('coverage-row-BBB')
    expect(bbb).toHaveTextContent('Material gaps (1% to 5%): results may be affected')
    expect(bbb).toHaveTextContent('1400')
    expect(bbb).toHaveTextContent('2.86%')
    expect(screen.getByTestId('coverage-row-CCC')).toHaveTextContent(
      'Minor gaps (up to 1% of sessions)'
    )
    expect(screen.getByTestId('coverage-row-AAA')).toHaveTextContent('Complete coverage')
    expect(screen.getByTestId('coverage-denominator')).toHaveTextContent(
      '40 of 4200 expected sessions (0.95%)'
    )
  })

  it('orders a severe symbol before a material one', () => {
    const [aaa, bbb, ccc] = COVERAGE.symbols
    const severe = { ...ccc, symbol: 'SSS', class: 'SEVERE_GAPS' as const }
    render(<SessionCoverageTable coverage={{ ...COVERAGE, symbols: [aaa, bbb, severe] }} />)
    const rows = within(screen.getByTestId('session-coverage-details')).getAllByRole('row').slice(1)
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'coverage-row-SSS',
      'coverage-row-BBB',
      'coverage-row-AAA',
    ])
    expect(screen.getByTestId('coverage-row-SSS')).toHaveTextContent(
      'Severe gaps (over 5%): not reliable historical validation'
    )
  })

  it('still renders a record without the denominator fields', () => {
    const { expected_sessions_after_exclusions_total: _t, ...universe } = COVERAGE.universe
    render(<SessionCoverageTable coverage={{ ...COVERAGE, universe }} />)
    expect(screen.queryByTestId('coverage-denominator')).not.toBeInTheDocument()
  })

  it('states the session basis and what it dropped, and hides the internal code', () => {
    render(<SessionCoverageTable coverage={COVERAGE} />)
    expect(screen.getByTestId('session-coverage')).toHaveTextContent(
      'Session basis: common sessions'
    )
    expect(screen.getByTestId('session-coverage')).toHaveTextContent('9 dropped by this basis')
    expect(screen.getByTestId('continuity-note')).not.toHaveTextContent('SESSION_AFTER_GAP')
    expect(screen.getByText(/2 further symbols/)).toBeInTheDocument()
  })

  it('reads not recorded for this run without the block', () => {
    render(<SessionCoverageTable coverage={null} />)
    expect(screen.getByTestId('session-coverage')).toHaveTextContent('not recorded for this run')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
