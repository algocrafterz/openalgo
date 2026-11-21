import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  consistency,
  dataQuality,
  riskBlock,
  summary,
  tradingBreakdowns,
} from '@/test/discretionaryFixtures'
import { render, screen, userEvent, within } from '@/test/test-utils'
import {
  DailyPnlChart,
  DataQualityStrip,
  HowYouTradeCard,
  RiskStrip,
  SampleNote,
} from './DiscretionaryInsights'

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
  it('says how many completed trades the ratios rest on and flags a low sample', () => {
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
    expect(note).toHaveTextContent('Ratios based on 3 completed trade(s) over 2 trading day(s).')
    expect(note).toHaveTextContent('Low sample: fewer than 30 completed trades or 10 trading days')
  })

  it('has no low-sample note once the rule is met, and renders nothing without data', () => {
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
    expect(screen.getByTestId('discretionary-sample-note')).not.toHaveTextContent('Low sample')
    rerender(<SampleNote sample={undefined} />)
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
      '2026-11-16: gross Rs 60.00; est. charges Rs 12.50; net Rs 47.50 (incomplete: some charges unavailable)'
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
  it('shows the figures and opens the largest win and loss', async () => {
    const onSelect = vi.fn()
    render(
      <RiskStrip consistency={consistency()} formatCurrency={money} onSelectTrade={onSelect} />
    )
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('Rs 30.00')
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('net Rs 23.75 (incomplete)')
    expect(screen.getByTestId('risk-days')).toHaveTextContent('1 / 1')
    expect(screen.getByTestId('risk-drawdown')).toHaveTextContent('Rs 40.00')
    expect(screen.getByTestId('risk-hold')).toHaveTextContent('45m')
    await userEvent.click(screen.getByRole('button', { name: /largest win, RELIANCE/ }))
    expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: 11, build_id: 'b1' }))
    await userEvent.click(screen.getByRole('button', { name: /largest loss, INFY/ }))
    expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: 12 }))
  })

  it('shows the largest win and loss on the net basis the server picks them on', () => {
    render(<RiskStrip consistency={consistency()} formatCurrency={money} onSelectTrade={vi.fn()} />)
    // RELIANCE: gross 100, net 87.50 -> the net is shown; INFY has no net -> gross, said so
    expect(screen.getByTestId('risk-largest-win')).toHaveTextContent('Rs 87.50')
    expect(screen.getByTestId('risk-largest-win')).toHaveTextContent('(net)')
    expect(screen.getByTestId('risk-largest-loss')).toHaveTextContent('Rs -40.00')
    expect(screen.getByTestId('risk-largest-loss')).toHaveTextContent('(gross)')
  })

  it('uses the net drawdown and streaks of the risk block when given, in rupees', () => {
    render(
      <RiskStrip
        consistency={consistency()}
        risk={riskBlock({ current_streak: { type: 'win', length: 1 } })}
        formatCurrency={money}
        onSelectTrade={vi.fn()}
      />
    )
    expect(screen.getByTestId('risk-drawdown')).toHaveTextContent('-Rs 320.50')
    expect(screen.getByTestId('risk-current-drawdown')).toHaveTextContent('-Rs 120.00')
    expect(screen.getByTestId('risk-current-streak')).toHaveTextContent('1 win')
    expect(screen.getByTestId('risk-win-streak')).toHaveTextContent('3')
    expect(screen.getByTestId('risk-strip').textContent).not.toMatch(/\d%/)
  })

  it('explains what cannot be calculated instead of showing a number', () => {
    render(
      <RiskStrip
        consistency={consistency({
          largest_win: null,
          trading_days: 1,
          avg_daily_pnl: null,
          avg_daily_net: null,
          avg_daily_unavailable_reason: 'needs at least 2 trading days',
          max_drawdown: null,
          max_drawdown_unavailable_reason: 'needs at least 2 trading days',
        })}
        formatCurrency={money}
        onSelectTrade={vi.fn()}
      />
    )
    expect(screen.getByTestId('risk-largest-win')).toHaveTextContent('no winning trade')
    expect(screen.getByTestId('risk-avg-daily')).toHaveTextContent('needs at least 2 trading days')
    expect(screen.getByTestId('risk-drawdown')).toHaveTextContent('needs at least 2 trading days')
    expect(screen.getByTestId('risk-drawdown')).not.toHaveTextContent('Rs 0.00')
  })
})

describe('HowYouTradeCard', () => {
  it('switches between time of day, weekday and holding time', async () => {
    render(
      <HowYouTradeCard
        breakdowns={tradingBreakdowns()}
        sample={{
          completed_trades: 2,
          trading_days: 1,
          min_trades: 30,
          min_days: 10,
          sufficient: false,
        }}
        formatCurrency={money}
      />
    )
    const card = screen.getByTestId('how-you-trade')
    expect(within(card).getByRole('columnheader', { name: 'Entry time (IST)' })).toBeInTheDocument()
    expect(within(card).getByText('09:15-10:00')).toBeInTheDocument()
    expect(within(card).getByText('Other (outside 09:15-15:30)')).toBeInTheDocument()
    // below the sample rule: the profit factor is shown with a muted low-sample marker and the
    // rule it misses in the title
    const pfCell = within(card).getAllByTitle(
      /Low sample: indicative only; needs at least 30 completed trades/
    )[0]
    expect(pfCell).toHaveTextContent('1.50')
    expect(within(pfCell).getByTestId('low-sample-mark')).toHaveTextContent('low sample')

    await userEvent.click(screen.getByRole('tab', { name: 'Weekday' }))
    expect(await within(card).findByText('Monday')).toBeInTheDocument()
    expect(within(card).getByText('1.50')).toBeInTheDocument() // sample_ok row shows it

    await userEvent.click(screen.getByRole('tab', { name: 'Holding time' }))
    expect(await within(card).findByText('15-60 min')).toBeInTheDocument()
    expect(within(card).getByText('Overnight')).toBeInTheDocument()
    for (const name of [
      'Trades',
      'Win rate',
      'Avg P&L (gross)',
      'Profit factor',
      'Net of charges',
    ]) {
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

describe('DataQualityStrip', () => {
  it('shows the four states, not placed, charges, prices and sync', () => {
    render(<DataQualityStrip dq={dataQuality()} />)
    expect(screen.getByTestId('dq-collected')).toHaveTextContent('1. Collected9')
    expect(screen.getByTestId('dq-awaiting')).toHaveTextContent('2. Awaiting attribution2')
    expect(screen.getByTestId('dq-awaiting')).toHaveTextContent('1 in the safety window, 1 other')
    expect(screen.getByTestId('dq-open')).toHaveTextContent('3')
    expect(screen.getByTestId('dq-included')).toHaveTextContent('4. In completed trades3')
    expect(screen.getByTestId('dq-not_placed')).toHaveTextContent('1')
    expect(screen.getByTestId('dq-charges')).toHaveTextContent(
      '1 trade(s) / 1 exit(s) without an estimate'
    )
    expect(screen.getByTestId('dq-prices')).toHaveTextContent('Prices not requested')
    expect(screen.getByTestId('dq-sync')).toHaveTextContent(
      'Ledger sync: last successful 2026-11-17 09:30 IST'
    )
    expect(screen.queryByText('counts do not add up')).not.toBeInTheDocument()
  })

  it('reports price problems only when prices were requested, and sync problems', () => {
    render(
      <DataQualityStrip
        dq={dataQuality({
          not_placed: 0,
          prices: {
            requested: true,
            open_positions: 2,
            statuses: { live: 1, unavailable: 1 },
            missing: 1,
            stale: 0,
          },
          sync: {
            last_successful_sync: null,
            days_without_sync: [],
            problems: ['latest ledger sync for broker x ended error'],
          },
        })}
      />
    )
    expect(screen.queryByTestId('dq-not_placed')).not.toBeInTheDocument()
    expect(screen.getByTestId('dq-prices')).toHaveTextContent('Prices: 1 without a usable price')
    expect(screen.getByTestId('dq-sync')).toHaveTextContent('no successful sync yet')
    expect(screen.getByTestId('dq-sync-problems')).toHaveTextContent('1 sync problem(s)')
  })
})
