import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  breakdownRow,
  consistency,
  riskBlock,
  summary,
  tradingBreakdowns,
} from '@/test/discretionaryFixtures'
import { render, screen, within } from '@/test/test-utils'
import { DiscretionaryBreakdowns } from './DiscretionaryBreakdowns'
import { RiskStrip, SampleNote } from './DiscretionaryInsights'

const money = (v: number) => `Rs ${v.toFixed(2)}`

let errorSpy: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  // no new console errors from any of these sections
  expect(errorSpy).not.toHaveBeenCalled()
  errorSpy.mockRestore()
})

describe('SampleNote', () => {
  it('says how many completed trades the ratios rest on, without a low-sample flag', () => {
    render(
      <SampleNote
        sample={{
          completed_trades: 3,
          trading_days: 2,
          min_trades: 30,
          min_days: 10,
          sufficient: false,
        }}
      />
    )
    const note = screen.getByTestId('discretionary-sample-note')
    expect(note).toHaveTextContent('Ratios based on 3 completed trades over 2 trading days.')
    expect(note).not.toHaveTextContent(/low sample|indicative/i)
  })

  it('states the same plain sentence for a large sample, and renders nothing without data or trades', () => {
    const { rerender } = render(
      <SampleNote
        sample={{
          completed_trades: 40,
          trading_days: 12,
          min_trades: 30,
          min_days: 10,
          sufficient: true,
        }}
      />
    )
    expect(screen.getByTestId('discretionary-sample-note')).toHaveTextContent(
      'Ratios based on 40 completed trades over 12 trading days.'
    )
    rerender(<SampleNote sample={undefined} />)
    expect(screen.queryByTestId('discretionary-sample-note')).not.toBeInTheDocument()
    rerender(
      <SampleNote
        sample={{
          completed_trades: 0,
          trading_days: 0,
          min_trades: 30,
          min_days: 10,
          sufficient: false,
        }}
      />
    )
    expect(screen.queryByTestId('discretionary-sample-note')).not.toBeInTheDocument()
  })
})

describe('RiskStrip', () => {
  it('shows the risk and consistency figures once, without the summary figures', () => {
    render(<RiskStrip consistency={consistency()} formatCurrency={money} />)
    // net first, like the rest of the page; the gross figure is the hint
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('Avg daily P&L (net)')
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('Rs 23.75')
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('incomplete')
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('gross Rs 30.00')
    expect(screen.getByTestId('risk-days')).toHaveTextContent('1 / 1')
    expect(screen.getByTestId('risk-hold')).toHaveTextContent('45m')
    // largest win / loss and the maximum drawdown live in the summary only
    expect(screen.queryByTestId('risk-largest-win')).not.toBeInTheDocument()
    expect(screen.queryByTestId('risk-largest-loss')).not.toBeInTheDocument()
    expect(screen.queryByTestId('risk-drawdown')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('uses the net streaks of the risk block when given, and states no drawdown (the summary does)', () => {
    render(
      <RiskStrip
        consistency={consistency()}
        risk={riskBlock({ current_streak: { type: 'win', length: 1 } })}
        formatCurrency={money}
      />
    )
    expect(screen.queryByTestId('risk-current-drawdown')).not.toBeInTheDocument()
    expect(screen.queryByTestId('risk-drawdown')).not.toBeInTheDocument()
    expect(screen.getByTestId('risk-strip')).not.toHaveTextContent(/drawdown (?!recovery)/i)
    expect(screen.getByTestId('risk-current-streak')).toHaveTextContent('1 win')
    expect(screen.getByTestId('risk-win-streak')).toHaveTextContent('3')
    expect(screen.getByTestId('risk-strip').textContent).not.toMatch(/\d%/)
  })

  it('explains what cannot be calculated instead of showing a number', () => {
    render(
      <RiskStrip
        consistency={consistency({
          trading_days: 1,
          avg_daily_pnl: null,
          avg_daily_net: null,
          avg_daily_unavailable_reason: 'needs at least 2 trading days',
        })}
        formatCurrency={money}
      />
    )
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('needs at least 2 trading days')
  })
})

describe('DiscretionaryBreakdowns (one card, every section stacked)', () => {
  const withRows = () =>
    summary({
      breakdowns: tradingBreakdowns(),
      by_symbol: [breakdownRow('RELIANCE'), breakdownRow('INFY')],
    })
  const tabs = ['symbol', 'time_of_day', 'weekday', 'hold_bucket'] as const

  it('shows every section given at once, in order, with its heading and table and no tabs', () => {
    render(<DiscretionaryBreakdowns summary={withRows()} tabs={[...tabs]} formatCurrency={money} />)
    const card = screen.getByTestId('breakdowns-card')
    expect(within(card).queryByRole('tab')).not.toBeInTheDocument()
    expect(within(card).queryByRole('tablist')).not.toBeInTheDocument()
    expect(
      within(card)
        .getAllByRole('heading', { level: 3 })
        .map((h) => h.textContent)
    ).toEqual(['Symbol', 'Time of day', 'Weekday', 'Holding time'])
    expect(card.querySelectorAll('table')).toHaveLength(4)
    for (const key of tabs) {
      const section = within(card).getByTestId(`breakdown-section-${key}`)
      expect(section.querySelectorAll('table')).toHaveLength(1)
    }
    // every table is readable at once: no click needed
    expect(within(card).getByText('RELIANCE')).toBeInTheDocument()
    expect(within(card).getByRole('columnheader', { name: 'Entry time (IST)' })).toBeInTheDocument()
    expect(within(card).getByText('09:15-10:00')).toBeInTheDocument()
    expect(
      within(card).getByText('Other (outside NSE cash hours, 09:15-15:30)')
    ).toBeInTheDocument()
    expect(within(card).getAllByText('1.50').length).toBeGreaterThan(0)
    expect(within(card).getByRole('columnheader', { name: 'Entry day' })).toBeInTheDocument()
    expect(within(card).getByText('Monday')).toBeInTheDocument()
    expect(within(card).getByText('15-60 min')).toBeInTheDocument()
    expect(within(card).getByText('Overnight')).toBeInTheDocument()
    // per-bucket sample, gross and charges columns are kept
    for (const name of [
      'Trades',
      'Win rate',
      'Avg P&L (net)',
      'Profit factor',
      'Gross P&L',
      'Est. charges',
      'Net of charges',
    ]) {
      expect(within(card).getAllByRole('columnheader', { name }).length).toBeGreaterThan(0)
    }
    // no second representation of the same rows: no bar chart
    expect(within(card).queryByTestId('time-of-day-chart')).not.toBeInTheDocument()
    expect(within(card).queryByTestId('net-bar')).not.toBeInTheDocument()
  })

  it('has a single weekday table, by entry day (the close-date weekday table is gone)', () => {
    render(
      <DiscretionaryBreakdowns
        summary={withRows()}
        tabs={['weekday', 'symbol']}
        formatCurrency={money}
      />
    )
    expect(screen.getAllByRole('heading', { name: /weekday/i })).toHaveLength(1)
    expect(screen.queryByText(/close date/i)).not.toBeInTheDocument()
  })

  it('renders only the sections it is given, in the fixed order, and nothing without any', () => {
    const { container, rerender } = render(
      <DiscretionaryBreakdowns
        summary={withRows()}
        tabs={['product', 'side']}
        formatCurrency={money}
      />
    )
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      'Long / Short',
      'Product',
    ])
    expect(screen.queryByRole('heading', { name: 'Symbol' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('breakdown-section-weekday')).not.toBeInTheDocument()
    rerender(<DiscretionaryBreakdowns summary={withRows()} tabs={[]} formatCurrency={money} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('says when a breakdown has no rows', () => {
    render(
      <DiscretionaryBreakdowns
        summary={summary({ breakdowns: { time_of_day: [], weekday: [], hold_bucket: [] } })}
        tabs={['time_of_day']}
        formatCurrency={money}
      />
    )
    expect(screen.getByText('None in this period')).toBeInTheDocument()
  })
})
