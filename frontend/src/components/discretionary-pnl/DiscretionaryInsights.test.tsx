import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { consistency, riskBlock, summary, tradingBreakdowns } from '@/test/discretionaryFixtures'
import { render, screen, userEvent, within } from '@/test/test-utils'
import { DailyPnlChart, HowYouTradeCard, RiskStrip, SampleNote } from './DiscretionaryInsights'

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

describe('DailyPnlChart', () => {
  it('draws one bar per day with charges and net in the tooltip, and counts the days', () => {
    const rows = summary().by_period.daily
    const second = {
      ...rows[0],
      period: '2026-11-17',
      label: '2026-11-17',
      realized_gross: -30,
      net_of_charges: -35,
      charges: 5,
      charges_unavailable_trades: 0,
      net_charges_complete: true,
    }
    render(<DailyPnlChart rows={[rows[0], second]} formatCurrency={money} />)
    const bars = screen.getAllByTestId('daily-pnl-bar')
    expect(bars).toHaveLength(2)
    expect(bars[0]).toHaveAttribute(
      'title',
      '2026-11-16: net Rs 47.50 (incomplete: some charges unavailable); gross Rs 60.00; est. charges Rs 12.50'
    )
    expect(bars[1].getAttribute('title')).toContain('gross Rs -30.00')
    expect(screen.getByTestId('daily-pnl-days')).toHaveTextContent('1 profitable / 1 losing day(s)')
    expect(screen.getByText(/same basis as the equity curve/)).toBeInTheDocument()
  })

  it('renders nothing without daily rows', () => {
    const { container } = render(<DailyPnlChart rows={[]} formatCurrency={money} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('RiskStrip', () => {
  it('shows the risk and consistency figures once, without the summary figures', () => {
    render(<RiskStrip consistency={consistency()} formatCurrency={money} />)
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('Rs 30.00')
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('net Rs 23.75 (incomplete)')
    expect(screen.getByTestId('risk-days')).toHaveTextContent('1 / 1')
    expect(screen.getByTestId('risk-hold')).toHaveTextContent('45m')
    // largest win / loss and the maximum drawdown live in the summary only
    expect(screen.queryByTestId('risk-largest-win')).not.toBeInTheDocument()
    expect(screen.queryByTestId('risk-largest-loss')).not.toBeInTheDocument()
    expect(screen.queryByTestId('risk-drawdown')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('uses the net current drawdown and streaks of the risk block when given, in rupees', () => {
    render(
      <RiskStrip
        consistency={consistency()}
        risk={riskBlock({ current_streak: { type: 'win', length: 1 } })}
        formatCurrency={money}
      />
    )
    expect(screen.getByTestId('risk-current-drawdown')).toHaveTextContent('-Rs 120.00')
    expect(screen.queryByTestId('risk-drawdown')).not.toBeInTheDocument()
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

describe('HowYouTradeCard', () => {
  it('switches between time of day, weekday and holding time', async () => {
    render(<HowYouTradeCard breakdowns={tradingBreakdowns()} formatCurrency={money} />)
    const card = screen.getByTestId('how-you-trade')
    expect(within(card).getByRole('columnheader', { name: 'Entry time (IST)' })).toBeInTheDocument()
    expect(within(card).getByText('09:15-10:00')).toBeInTheDocument()
    expect(
      within(card).getByText('Other (outside NSE cash hours, 09:15-15:30)')
    ).toBeInTheDocument()
    // the profit factor is shown plainly: no low-sample marker and no rule in the card
    expect(within(card).queryByTestId('low-sample-mark')).not.toBeInTheDocument()
    expect(card).not.toHaveTextContent(/low sample|indicative|edge-check/i)
    expect(within(card).getAllByText('1.50').length).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole('tab', { name: 'Weekday' }))
    expect(await within(card).findByText('Monday')).toBeInTheDocument()
    expect(within(card).getByText('1.50')).toBeInTheDocument() // the weekday row's profit factor is shown plainly

    await userEvent.click(screen.getByRole('tab', { name: 'Holding time' }))
    expect(await within(card).findByText('15-60 min')).toBeInTheDocument()
    expect(within(card).getByText('Overnight')).toBeInTheDocument()
    for (const name of ['Trades', 'Win rate', 'Avg P&L (net)', 'Profit factor', 'Net of charges']) {
      expect(within(card).getByRole('columnheader', { name })).toBeInTheDocument()
    }
  })

  it('says when a breakdown has no rows', () => {
    render(
      <HowYouTradeCard
        breakdowns={{ time_of_day: [], weekday: [], hold_bucket: [] }}
        formatCurrency={money}
      />
    )
    expect(screen.getByText('None in this period')).toBeInTheDocument()
  })
})
