import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { makeCard, makeLatest } from '@/test/backtestResultsFixtures'
import { CoverageChip, DataCoverageTrust } from './CoverageChip'
import { COVERAGE, WARNINGS } from './fixtures/engineBlocks'
import { StrategyCatalogue } from './StrategyCatalogue'

describe('DataCoverageTrust', () => {
  it('shows the chip, a plain sentence and the one-line warning note', () => {
    render(<DataCoverageTrust coverage={COVERAGE} warnings={WARNINGS} />)
    expect(screen.getByTestId('coverage-chip')).toHaveTextContent(
      'Material gaps (1% to 5%): results may be affected'
    )
    expect(screen.getByTestId('coverage-sentence')).toHaveTextContent(
      '40 of 4200 expected sessions (0.95%)'
    )
    expect(screen.getByTestId('coverage-sentence')).toHaveTextContent(
      'symbols: 1 complete, 1 minor gaps, 1 material gaps, 0 severe gaps'
    )
    const note = screen.getByTestId('execution-warning-note')
    expect(note).toHaveTextContent('12 of 200 trades (6.0%) touched both stop and target')
    expect(note).toHaveTextContent('4 of 200 trades (2.0%) were open while a bar was missing')
  })

  it.each([
    ['COMPLETE', 'Complete coverage'],
    ['MINOR_GAPS', 'Minor gaps (up to 1% of sessions)'],
    ['MATERIAL_GAPS', 'Material gaps (1% to 5%): results may be affected'],
    ['SEVERE_GAPS', 'Severe gaps (over 5%): not reliable historical validation'],
  ] as const)('labels %s in trader words', (cls, label) => {
    render(<CoverageChip coverageClass={cls} />)
    expect(screen.getByTestId('coverage-chip')).toHaveTextContent(label)
  })

  it('shows the severe chip in the strongest failure styling', () => {
    render(<CoverageChip coverageClass="SEVERE_GAPS" />)
    const chip = screen.getByTestId('coverage-chip')
    expect(chip.className).toContain('border-red-500/60')
    expect(chip.className).toContain('text-red-700')
  })

  it('shows the recorded disclosure verbatim for material and severe runs', () => {
    const severe =
      'Many trading sessions are missing for part of this basket. Treat this result as indicative only, not as reliable historical validation.'
    const { rerender } = render(<DataCoverageTrust coverage={COVERAGE} warnings={null} />)
    expect(screen.getByTestId('coverage-disclosure').textContent).toBe(COVERAGE.disclosure)
    rerender(
      <DataCoverageTrust
        coverage={{ ...COVERAGE, coverage_class: 'SEVERE_GAPS', disclosure: severe }}
        warnings={null}
      />
    )
    expect(screen.getByTestId('coverage-disclosure').textContent).toBe(severe)
  })

  it.each(['COMPLETE', 'MINOR_GAPS'] as const)('shows no disclosure for %s', (cls) => {
    render(
      <DataCoverageTrust
        coverage={{ ...COVERAGE, coverage_class: cls, disclosure: undefined }}
        warnings={null}
      />
    )
    expect(screen.queryByTestId('coverage-disclosure')).not.toBeInTheDocument()
  })

  it('shows no warning note when no trade is affected', () => {
    render(
      <DataCoverageTrust
        coverage={{ ...COVERAGE, coverage_class: 'COMPLETE' }}
        warnings={{ ...WARNINGS, ambiguous_exit_trades: 0, data_gap_trades: 0 }}
      />
    )
    expect(screen.queryByTestId('execution-warning-note')).not.toBeInTheDocument()
  })

  it('says not recorded for this run when the record has neither block', () => {
    render(<DataCoverageTrust coverage={null} warnings={null} />)
    expect(screen.getByTestId('coverage-sentence')).toHaveTextContent('not recorded for this run')
    expect(screen.queryByTestId('coverage-chip')).not.toBeInTheDocument()
    expect(screen.queryByTestId('execution-warning-note')).not.toBeInTheDocument()
  })
})

describe('catalogue coverage chip', () => {
  const renderRow = (card: ReturnType<typeof makeCard>) =>
    render(
      <MemoryRouter>
        <StrategyCatalogue ideas={[card]} />
      </MemoryRouter>
    )

  it('shows a chip when the latest run carries the block', () => {
    renderRow(makeCard({ latest_run: makeLatest({ session_coverage: COVERAGE }) }))
    expect(screen.getByTestId('catalogue-coverage')).toHaveTextContent(
      'Material gaps (1% to 5%): results may be affected'
    )
    expect(screen.getByTestId('catalogue-coverage-denominator')).toHaveTextContent(
      '40 of 4200 expected sessions (0.95%)'
    )
  })

  it('shows nothing when the latest run lacks it', () => {
    renderRow(makeCard())
    expect(screen.queryByTestId('catalogue-coverage')).not.toBeInTheDocument()
  })
})
