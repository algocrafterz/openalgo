import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PortfolioView } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import { StatisticsTable, statisticGroups } from './StatisticsTable'

const COST = {
  modelled_net_pnl_rs: 4828370.84,
  modelled_total_cost_rs: 91046.85,
  pnl_basis: 'modelled',
}
const BOOK: PortfolioView = {
  source: 'recorded',
  stats: {
    annual_return_pct: 10.6,
    max_drawdown_pct: -23,
    max_drawdown_peak: '2020-02-14',
    max_drawdown_trough: '2020-03-23',
    sharpe: 1.08,
  },
  monthly_pct: [
    { month: '2020-01', return_pct: 1.5 },
    { month: '2020-02', return_pct: -9.7 },
    { month: '2020-03', return_pct: 10.7 },
  ],
}

describe('statisticGroups', () => {
  it('reads every figure from the run as recorded, grouped as the table shows them', () => {
    const groups = statisticGroups(
      makeRun({
        cost_model: COST,
        metrics: {
          trades: 301,
          gross_bps: 1634,
          net_bps: 1610.36,
          payoff: 1.14,
          expectancy_r: -0.012,
          t_stat: 5.66,
          hurdle_t: 1.97,
        },
      }),
      BOOK
    )
    expect(groups.map((g) => g.title)).toEqual(['Performance', 'Trades', 'Risk'])
    const rows = Object.fromEntries(groups.flatMap((g) => g.rows).map((r) => [r.key, r.value]))
    expect(rows.net).toBe('+Rs 48,28,371')
    expect(rows.costs).toBe('Rs 91,047')
    // gross is the recorded net plus the recorded total cost, not a new calculation
    expect(rows.gross).toBe('+Rs 49,19,418')
    expect(rows.payoff).toBe('1.14')
    expect(rows.months).toBe('+10.7% / -9.7%')
    expect(rows.drawdown).toBe('-23.0%')
    expect(rows.sharpe).toBe('1.08')
  })

  it('leaves out what the run did not record instead of showing zero', () => {
    const groups = statisticGroups(makeRun({ metrics: { trades: 0, gross_bps: 0, net_bps: 0 } }))
    const keys = groups.flatMap((g) => g.rows).map((r) => r.key)
    expect(keys).not.toContain('net')
  })

  it('falls back to the drawdown in R and the monthly R when there is no book', () => {
    const groups = statisticGroups(makeRun())
    const rows = Object.fromEntries(groups.flatMap((g) => g.rows).map((r) => [r.key, r.value]))
    expect(rows.drawdown).toBe('12.5R')
    expect(rows.months).toBe('+0.5R / -1.2R')
  })
})

describe('StatisticsTable', () => {
  it('colours only a result that is a gain or a loss', () => {
    render(<StatisticsTable run={makeRun({ cost_model: COST })} portfolio={BOOK} />)
    expect(within(screen.getByTestId('stat-net')).getByText('+Rs 48,28,371')).toHaveClass(
      'text-green-600'
    )
    expect(within(screen.getByTestId('stat-costs')).getByText('Rs 91,047')).not.toHaveClass(
      'text-green-600',
      'text-red-600'
    )
    expect(within(screen.getByTestId('stat-drawdown')).getByText('-23.0%')).toHaveClass(
      'text-red-600'
    )
    expect(screen.getByTestId('stat-drawdown')).toHaveTextContent('2020-02-14 to 2020-03-23')
  })

  it('says so when no statistics were recorded', () => {
    render(
      <StatisticsTable
        run={makeRun({ metrics: { trades: 0, gross_bps: 0, net_bps: 0 }, series: undefined })}
      />
    )
    expect(screen.getByTestId('statistics-empty')).toBeInTheDocument()
  })
})
