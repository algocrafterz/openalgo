import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type {
  ExcursionPosition,
  ExcursionsResponse,
  InstrumentExcursions,
} from '@/api/discretionaryPnl'
import { excursionPosition, excursions, trade } from '@/test/discretionaryFixtures'
import { render, screen, within } from '@/test/test-utils'
import { ClosedTradesTable } from './DiscretionaryTables'
import { ExcursionPanel } from './ExcursionPanel'

const money = (v: number) => `Rs ${v.toFixed(2)}`
const noop = () => {}

const dist = (average: number | null, median: number | null, count = 1) => ({
  count,
  average,
  median,
})
const group = (count: number) => ({
  mae_points: dist(2, 2, count),
  mfe_points: dist(14, 14, count),
  mae_amount: dist(25, 25, count),
  mfe_amount: dist(140, 140, count),
})

function instrument(symbol: string, extra: Partial<InstrumentExcursions> = {}) {
  return {
    symbol,
    exchange: 'NSE',
    available: 13,
    winners: group(7),
    losers: group(6),
    capture: {
      positions: 13,
      captured: 7,
      excluded: { not_net_winner: 6 },
      excluded_total: 6,
      median_capture: 0.6243,
      median_giveback_points: 8.7833,
      mae_over_half: { count: 4, denominator: 11, share: 0.3636 },
      basis: 'gross points per unit, average entry in force; winners by net P&L',
    },
    ...extra,
  } as InstrumentExcursions
}

function response(instruments: InstrumentExcursions[], positions: ExcursionPosition[]) {
  const base = excursions()
  const multi = instruments.length > 1
  return {
    ...base,
    positions,
    aggregates: {
      ...base.aggregates,
      instrument_count: instruments.length,
      instruments,
      winners: {
        ...base.aggregates.winners,
        mae_points: multi ? null : base.aggregates.winners.mae_points,
        mfe_points: multi ? null : base.aggregates.winners.mfe_points,
        profit_capture: multi ? null : base.aggregates.winners.profit_capture,
      },
      losers: {
        ...base.aggregates.losers,
        mae_points: multi ? null : base.aggregates.losers.mae_points,
        mfe_points: multi ? null : base.aggregates.losers.mfe_points,
      },
    },
  } as ExcursionsResponse
}

function panel(data: ExcursionsResponse) {
  return render(
    <ExcursionPanel
      data={data}
      loading={false}
      failed={false}
      onRetry={noop}
      formatCurrency={money}
    />
  )
}

const tcs = (id: number, extra: Partial<ExcursionPosition> = {}) =>
  excursionPosition({ id, symbol: 'TCS', ...extra })

describe('MAE / MFE lower-bound wording', () => {
  it('says both are conservative lower bounds and why', () => {
    panel(response([instrument('TCS')], [tcs(1), tcs(2)]))
    const note = screen.getByTestId('excursions-lower-bound')
    expect(note).toHaveTextContent('conservative lower bounds')
    expect(note).toHaveTextContent('fill price')
    expect(screen.getAllByTestId('excursion-row')[0]).toHaveTextContent('lower bound')
  })

  it('MAE and MFE tooltips say the rupee figure uses the quantity open at that moment', async () => {
    const t = trade({ id: 11 })
    render(
      <ClosedTradesTable
        trades={[t]}
        formatCurrency={money}
        onSelect={noop}
        excursions={new Map([[11, excursionPosition({ id: 11 })]])}
        excursionsLoaded
      />
    )
    await userEvent.click(screen.getByRole('button', { name: /details of trade \d+/ }))
    const detail = screen.getByTestId('closed-trade-detail')
    const mae = within(detail).getByTestId('mae-value').getAttribute('title') ?? ''
    const mfe = within(detail).getByTestId('mfe-value').getAttribute('title') ?? ''
    for (const title of [mae, mfe]) {
      expect(title).toContain('quantity open at that moment')
      expect(title).toContain('lower bound')
      expect(title).not.toContain('full size')
    }
  })
})

describe('capture per instrument', () => {
  it('shows median capture, median giveback and MAE above half with n of N beside each', () => {
    panel(response([instrument('TCS')], [tcs(1), tcs(2)]))
    const row = screen.getByTestId('capture-row')
    expect(row).toHaveTextContent('TCS (NSE)')
    expect(screen.getByTestId('capture-n')).toHaveTextContent('n = 7 of 13')
    expect(screen.getByTestId('capture-median')).toHaveTextContent('62%')
    expect(screen.getByTestId('giveback-median')).toHaveTextContent('8.78')
    expect(screen.getByTestId('mae-half')).toHaveTextContent('4 of 11 (36%)')
    expect(screen.getByTestId('capture-excluded')).toHaveTextContent('6 not a net winner')
    const basis = screen.getByTestId('capture-basis')
    expect(basis).toHaveTextContent('Gross points per unit')
    expect(basis).toHaveTextContent('net winners')
    expect(basis.textContent ?? '').not.toMatch(/significan|reliable trend|proves/i)
  })

  it('keeps instruments apart: one row each, no pooled row, no pooled points', () => {
    const data = response(
      [
        instrument('INFY', {
          capture: {
            positions: 3,
            captured: 2,
            excluded: { insufficient_candle_coverage: 1 },
            excluded_total: 1,
            median_capture: 0.9,
            median_giveback_points: 1.5,
            mae_over_half: { count: 0, denominator: 2, share: 0 },
            basis: 'gross points per unit, average entry in force; winners by net P&L',
          },
        }),
        instrument('TCS'),
      ],
      [tcs(1), tcs(2, { symbol: 'INFY' })]
    )
    panel(data)
    const rows = screen.getAllByTestId('capture-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('INFY (NSE)')
    expect(rows[0]).toHaveTextContent('n = 2 of 3')
    expect(rows[1]).toHaveTextContent('n = 7 of 13')
    expect(screen.getByTestId('excursions-points-per-instrument')).toHaveTextContent('never added')
    // the scope-level table carries rupees only; points come once per instrument
    const scope = screen.getAllByTestId('excursion-table')[0]
    expect(scope).toHaveTextContent('Rs')
    expect(scope).not.toHaveTextContent('points per unit')
    expect(screen.getAllByTestId('instrument-points')).toHaveLength(2)
    // the pooled scatter is not drawn across instruments
    expect(screen.queryByTestId('mae-mfe-scatter')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('capture-excluded')[0]).toHaveTextContent(
      'no full candle between first and last fill'
    )
  })

  it('a single instrument keeps the points table and names the instrument', () => {
    panel(response([instrument('TCS')], [tcs(1), tcs(2)]))
    expect(screen.getByTestId('excursion-table')).toHaveTextContent('Per position, TCS (NSE)')
    expect(screen.getByText('Winners average (1 positions)')).toBeInTheDocument()
    expect(screen.queryByTestId('excursions-points-per-instrument')).not.toBeInTheDocument()
  })

  it('shows dashes, never zero, when no position qualified', () => {
    const none = instrument('TCS', {
      capture: {
        positions: 2,
        captured: 0,
        excluded: { not_net_winner: 2 },
        excluded_total: 2,
        median_capture: null,
        median_giveback_points: null,
        mae_over_half: { count: 0, denominator: 0, share: null },
        basis: 'x',
      },
    })
    panel(response([none], [tcs(1), tcs(2)]))
    expect(screen.getByTestId('capture-n')).toHaveTextContent('n = 0 of 2')
    for (const id of ['capture-median', 'giveback-median', 'mae-half']) {
      expect(screen.getByTestId(id)).toHaveTextContent('—')
      expect(screen.getByTestId(id).textContent).not.toMatch(/0/)
    }
  })

  it('an older server without the new fields still renders: no capture table, no crash', () => {
    panel(excursions())
    expect(screen.getByTestId('excursion-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('capture-by-instrument')).not.toBeInTheDocument()
    expect(screen.getByTestId('excursions-lower-bound')).toBeInTheDocument()
  })

  it('an older server pooling several instruments shows rupees only, no points', () => {
    const old = excursions()
    old.positions = [excursionPosition({ id: 1 }), excursionPosition({ id: 2, symbol: 'TCS' })]
    panel(old)
    expect(screen.getByTestId('excursion-table')).not.toHaveTextContent('points per unit')
    expect(screen.queryByTestId('mae-mfe-scatter')).not.toBeInTheDocument()
  })
})

describe('capture per position', () => {
  function open(position: ExcursionPosition | undefined) {
    render(
      <ClosedTradesTable
        trades={[trade({ id: 11 })]}
        formatCurrency={money}
        onSelect={noop}
        excursions={position ? new Map([[11, position]]) : undefined}
        excursionsLoaded
      />
    )
    return userEvent
      .click(screen.getByRole('button', { name: /details of trade \d+/ }))
      .then(() => screen.getByTestId('closed-trade-detail'))
  }

  it('shows capture and giveback for an eligible position', async () => {
    const detail = await open(
      excursionPosition({ id: 11, profit_capture: 0.5602, giveback_points: 15.8535 })
    )
    expect(within(detail).getByTestId('capture-value')).toHaveTextContent('56%')
    expect(within(detail).getByTestId('giveback-value')).toHaveTextContent('15.85')
  })

  it('shows a dash with the reason for a position that is not counted, never zero', async () => {
    const detail = await open(
      excursionPosition({
        id: 11,
        profit_capture: null,
        giveback_points: null,
        capture_reason: 'not_net_winner',
        capture_reason_text: 'not a net winner',
      })
    )
    const dash = within(detail).getByTestId('capture-unavailable')
    expect(dash).toHaveTextContent('—')
    expect(dash).toHaveAttribute('title', 'not a net winner')
    expect(within(detail).getByTestId('giveback-unavailable')).toHaveTextContent('—')
    expect(within(detail).queryByTestId('capture-value')).not.toBeInTheDocument()
  })

  it('shows a dash when the excursions did not load', async () => {
    const detail = await open(undefined)
    expect(within(detail).getByTestId('capture-unavailable')).toHaveTextContent('—')
  })
})
