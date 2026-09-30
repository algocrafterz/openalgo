import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PositionCap } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import { RealityChecks } from './RealityChecks'

const CAPS: PositionCap[] = [
  { max_positions: 10, trades: 800, taken_pct: 15.6, gross_bps: 5, net_bps: -11, t_stat: -0.4 },
  { max_positions: 3, trades: 400, taken_pct: 7.8, gross_bps: 6, net_bps: 95.9, t_stat: 0.9 },
  { max_positions: 5, trades: 600, taken_pct: 11.7, gross_bps: 7, net_bps: 133.4, t_stat: 1.0 },
]

const BASELINE = {
  gross_bps: 30,
  net_bps: 6,
  trade_net_bps: 130.8,
  excess_net_bps: 124.8,
  excess_t_stat: 2.1,
  trades: 5115,
  skipped: 0,
  method: 'same-symbol random entry, same holding period in bars, open to close',
}

function withMetrics(extra: Record<string, number>) {
  const base = makeRun()
  return { ...base.metrics, ...extra }
}

describe('position caps', () => {
  it('lists each slot count in ascending order with trades taken and net bps', () => {
    render(<RealityChecks run={makeRun({ position_caps: CAPS })} />)
    const rows = within(screen.getByTestId('position-caps')).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getAllByRole('cell')[0].textContent)).toEqual([
      '3',
      '5',
      '10',
      'Unlimited',
    ])
    expect(within(rows[0]).getAllByRole('cell')[1]).toHaveTextContent('400')
    expect(within(rows[0]).getAllByRole('cell')[3]).toHaveTextContent('95.90')
  })

  it('shows the unlimited headline as the last row for comparison', () => {
    render(<RealityChecks run={makeRun({ position_caps: CAPS })} />)
    const rows = within(screen.getByTestId('position-caps')).getAllByRole('row')
    expect(rows[rows.length - 1]).toHaveTextContent('-1.30')
  })

  it('says so when caps were not recorded', () => {
    render(<RealityChecks run={makeRun()} />)
    expect(screen.getByTestId('no-caps')).toHaveTextContent(/not recorded/i)
  })

  it('warns when the strategy needs more than ten positions at once', () => {
    const run = makeRun({
      position_caps: CAPS,
      metrics: withMetrics({ peak_concurrent: 174, median_concurrent: 72 }),
    })
    render(<RealityChecks run={run} />)
    const note = screen.getByTestId('slot-warning')
    expect(note).toHaveTextContent('174')
    expect(note).toHaveTextContent('72')
    expect(note).toHaveTextContent(/unlimited/i)
  })

  it('does not warn when ten slots or fewer are enough', () => {
    const run = makeRun({ position_caps: CAPS, metrics: withMetrics({ peak_concurrent: 8 }) })
    render(<RealityChecks run={run} />)
    expect(screen.queryByTestId('slot-warning')).not.toBeInTheDocument()
  })
})

describe('random-entry baseline', () => {
  it('shows the strategy, the random baseline and the excess with its t-statistic', () => {
    render(<RealityChecks run={makeRun({ baseline: BASELINE })} />)
    const box = screen.getByTestId('baseline')
    expect(box).toHaveTextContent('130.80')
    expect(box).toHaveTextContent('6.00')
    expect(box).toHaveTextContent('+124.80')
    expect(box).toHaveTextContent('2.10')
    expect(box).toHaveTextContent(/open to close/)
  })

  it('describes a negative excess without praising the strategy', () => {
    const run = makeRun({ baseline: { ...BASELINE, excess_net_bps: -54.4, excess_t_stat: -3.1 } })
    render(<RealityChecks run={run} />)
    expect(screen.getByTestId('baseline')).toHaveTextContent(/less than random entry/i)
  })

  it('describes a positive excess neutrally', () => {
    render(<RealityChecks run={makeRun({ baseline: BASELINE })} />)
    expect(screen.getByTestId('baseline')).toHaveTextContent(/more than random entry/i)
  })

  it('explains the absence of a baseline', () => {
    render(<RealityChecks run={makeRun()} />)
    expect(screen.getByTestId('no-baseline')).toHaveTextContent(/not computed/i)
  })

  it('reports trades that could not be matched', () => {
    render(<RealityChecks run={makeRun({ baseline: { ...BASELINE, skipped: 12 } })} />)
    expect(screen.getByTestId('baseline')).toHaveTextContent(/12 trades could not be matched/)
  })
})

describe('sign disagreement', () => {
  it('explains a positive per-trade result beside a negative t-statistic', () => {
    const run = makeRun({ metrics: withMetrics({ net_bps: 17, t_stat: -2.2 }) })
    render(<RealityChecks run={run} />)
    expect(screen.getByTestId('sign-note')).toHaveTextContent(/heavy signal days/i)
  })

  it('stays quiet when the two agree', () => {
    const run = makeRun({ metrics: withMetrics({ net_bps: 17, t_stat: 2.2 }) })
    render(<RealityChecks run={run} />)
    expect(screen.queryByTestId('sign-note')).not.toBeInTheDocument()
  })
})
