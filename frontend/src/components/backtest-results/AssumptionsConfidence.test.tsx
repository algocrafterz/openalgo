import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { makeRun, makeSwingDetail } from '@/test/backtestResultsFixtures'
import { AssumptionsConfidence, KNOWN_LIMITATIONS } from './AssumptionsConfidence'
import { ENGINE_BLOCKS } from './fixtures/engineBlocks'

describe('AssumptionsConfidence', () => {
  it('splits the statements into four groups and states the engine rules', () => {
    render(<AssumptionsConfidence run={makeRun()} detail={makeSwingDetail()} />)
    for (const g of [
      'Calculated from data',
      'Simulated by the engine',
      'Depends on an assumption',
      'Not modelled',
    ]) {
      expect(screen.getByTestId(`assumption-group-${g}`)).toBeInTheDocument()
    }
    const simulated = screen.getByTestId('assumption-group-Simulated by the engine')
    expect(simulated).toHaveTextContent("filled at the next bar's open")
    expect(simulated).toHaveTextContent('the stop is taken')
    expect(simulated).toHaveTextContent('fill is the open price')
    const none = screen.getByTestId('assumption-group-Not modelled')
    for (const t of [
      'Partial fills',
      'Queue position',
      'Limit-order fills',
      'price bands',
      'Corporate actions',
    ]) {
      expect(none).toHaveTextContent(t)
    }
  })

  it('reads not recorded for anything the run does not carry', () => {
    render(<AssumptionsConfidence run={makeRun()} detail={null} />)
    const assumed = screen.getByTestId('assumption-group-Depends on an assumption')
    expect(within(assumed).getByText(/Slippage per side/).parentElement).toHaveTextContent(
      'not recorded'
    )
    expect(
      within(assumed).getByText(/Position sizing and capital/).parentElement
    ).toHaveTextContent('not recorded')
    expect(screen.getByTestId('confidence-section')).toHaveTextContent('Data checks: not recorded')
  })

  it('reports recorded slippage and the cost model', () => {
    const detail = makeSwingDetail()
    const withSlip = {
      ...detail,
      scope: { ...(detail.scope as NonNullable<typeof detail.scope>), slippage_bps_per_side: 4 },
    }
    render(<AssumptionsConfidence run={makeRun()} detail={withSlip} />)
    const assumed = screen.getByTestId('assumption-group-Depends on an assumption')
    expect(assumed).toHaveTextContent('4.0 bps')
    expect(assumed).toHaveTextContent('zerodha-mis, 9.4 bps round trip')
  })

  it('shows the short form with a link to the full panel', () => {
    render(<AssumptionsConfidence run={makeRun()} detail={null} compact />)
    expect(screen.getByTestId('assumptions-compact')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'All assumptions' })).toHaveAttribute(
      'href',
      '#assumptions-confidence'
    )
  })

  describe('engine 1.6 rows', () => {
    const group = () =>
      screen.getByTestId('assumption-group-Coverage, look-ahead protection and versions')

    it('shows coverage, look-ahead protection and versions from the record', () => {
      render(<AssumptionsConfidence run={makeRun(ENGINE_BLOCKS)} detail={null} />)
      const g = group()
      expect(g).toHaveTextContent('2021-01-04 to 2026-09-25, 1380 sessions')
      expect(g).toHaveTextContent(
        'common sessions (only days every symbol has), 9 sessions dropped'
      )
      expect(g).toHaveTextContent('Material gaps (1% to 5%): results may be affected')
      expect(g).toHaveTextContent('40 whole sessions with no bars at all')
      expect(g).toHaveTextContent('of 4200 expected sessions after exclusions')
      expect(g).toHaveTextContent('55 bars missing inside 6 sessions that do exist')
      expect(g).toHaveTextContent('Checked: the guard watched every bar of 40 symbols')
      expect(g).toHaveTextContent('Re-run test on a sample of symbols: passed')
      expect(g).toHaveTextContent('Engine version: 1.6')
      expect(g).toHaveTextContent('costs priced on the real buy and sell leg')
    })

    it('says the look-ahead check was not run', () => {
      const guard = { ...ENGINE_BLOCKS.lookahead_guard, lookahead_check: 'not_run' as const }
      render(
        <AssumptionsConfidence
          run={makeRun({ ...ENGINE_BLOCKS, lookahead_guard: guard as never })}
          detail={null}
        />
      )
      expect(group()).toHaveTextContent('Re-run test on a sample of symbols: not run')
    })

    it('reads not recorded for this run for an older record', () => {
      render(<AssumptionsConfidence run={makeRun()} detail={null} />)
      const g = group()
      for (const label of [
        'Effective test period',
        'Session basis',
        'Coverage',
        'Missing full sessions',
        'Look-ahead protection',
        'Engine version',
        'Cost model version',
      ]) {
        expect(within(g).getByText(new RegExp(`^${label}:`)).parentElement).toHaveTextContent(
          'not recorded for this run'
        )
      }
    })

    it('lists the six known limitations and the unmodelled items on every run', () => {
      for (const run of [makeRun(), makeRun(ENGINE_BLOCKS)]) {
        const { unmount } = render(<AssumptionsConfidence run={run} detail={null} />)
        const box = screen.getByTestId('known-limitations')
        expect(box).toHaveTextContent('Known limitations of this backtester')
        expect(within(box).getAllByRole('listitem')).toHaveLength(6)
        expect(KNOWN_LIMITATIONS).toHaveLength(6)
        expect(box).toHaveTextContent('not every unsupported way of reading data')
        expect(box).toHaveTextContent('run strategy code without the guard')
        expect(box).toHaveTextContent('only caught by that sample re-run')
        expect(box).toHaveTextContent('Swing runs do not detect a missing bar before a fill')
        expect(box).toHaveTextContent(
          'priced as one sell order, so its brokerage is slightly understated'
        )
        expect(box).toHaveTextContent('flagged as uncertain')
        expect(screen.getByTestId('assumption-group-Not modelled')).toHaveTextContent(
          'Moving averages are not reset across a missing session'
        )
        unmount()
      }
    })
  })
})
