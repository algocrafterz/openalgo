import { describe, expect, it } from 'vitest'
import {
  breakdownVisibility,
  chargesInsightHint,
  largestLossHint,
} from '@/lib/discretionaryPnlView'
import {
  breakdownRow,
  calendarRow,
  consistency,
  costsBlock,
  excursionPosition,
  excursions,
  riskBlock,
  summary,
} from '@/test/discretionaryFixtures'
import { render, screen } from '@/test/test-utils'
import { CalendarTable } from './DiscretionaryBlocks'
import { RiskStrip } from './DiscretionaryInsights'
import { DiscretionaryStatistics, KeyFigures } from './DiscretionaryStatistics'
import { ExcursionPanel } from './ExcursionPanel'

const money = (v: number) => `Rs ${v.toFixed(2)}`
const noop = () => {}

function panel(data = excursions()) {
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

describe('MAE / MFE shows its coverage at the point of use', () => {
  it('states the included count of the total and the minute-bar basis', () => {
    panel()
    const line = screen.getByTestId('excursions-coverage')
    expect(line).toHaveTextContent('Based on 1 of 2 completed positions with 1-minute price data')
    expect(line).toHaveTextContent('bounds from minute bars, not exact')
    expect(line).toHaveTextContent('the other positions are not included')
    expect(screen.getByText('Winners average (1 positions)')).toBeInTheDocument()
  })

  it('does not say the other positions are left out when all are included', () => {
    const full = excursions()
    full.aggregates.coverage = {
      available: 2,
      total: 2,
      label: '2 of 2 positions available',
      unavailable_reasons: {},
    }
    panel(full)
    expect(screen.getByTestId('excursions-coverage')).toHaveTextContent('2 of 2 completed')
    expect(screen.getByTestId('excursions-coverage')).not.toHaveTextContent('not included')
  })

  it('draws no scatter for a single plotted position', () => {
    panel()
    expect(screen.queryByTestId('mae-mfe-scatter')).not.toBeInTheDocument()
    expect(screen.queryByTestId('mae-mfe-scatter-empty')).not.toBeInTheDocument()
  })

  it('draws the scatter from two plotted positions', () => {
    panel(
      excursions({
        positions: [excursionPosition({ id: 1 }), excursionPosition({ id: 2, net_pnl: -30 })],
      })
    )
    expect(screen.getByTestId('mae-mfe-scatter')).toBeInTheDocument()
  })

  it('shows no table, no zero and no chart when no position has price data', () => {
    const none = excursions({ positions: [] })
    none.aggregates.coverage = {
      available: 0,
      total: 3,
      label: '0 of 3 positions available',
      unavailable_reasons: { no_price_data: 3 },
    }
    panel(none)
    expect(screen.getByTestId('excursions-none')).toHaveTextContent(
      'MAE / MFE is not shown: 0 of 3 completed positions have 1-minute price bars'
    )
    expect(screen.queryByTestId('excursion-row')).not.toBeInTheDocument()
    expect(screen.queryByTestId('excursions-coverage')).not.toBeInTheDocument()
  })

  it('does not show exit efficiency (profit capture) or the server availability note', () => {
    panel()
    expect(screen.queryByTestId('profit-capture-row')).not.toBeInTheDocument()
    expect(screen.queryByTestId('profit-capture-block')).not.toBeInTheDocument()
    expect(screen.queryByTestId('excursions-note')).not.toBeInTheDocument()
  })
})

describe('unsupported items are not rendered', () => {
  it('shows no gross drawdown stand-in when the net risk block is missing', () => {
    render(<KeyFigures summary={{ ...summary(), risk: null }} formatCurrency={money} />)
    expect(screen.queryByTestId('summary-drawdown')).not.toBeInTheDocument()
    expect(screen.queryByText(/Max drawdown/)).not.toBeInTheDocument()
    // the figures that remain in the row are still there
    expect(screen.getByText('Expectancy (net)')).toBeInTheDocument()
    expect(screen.getByText('Profit factor (net)')).toBeInTheDocument()
  })

  it('labels the net drawdown with the number of trades it rests on', () => {
    render(
      <KeyFigures
        summary={{ ...summary(), risk: riskBlock({ positions: 7 }) }}
        formatCurrency={money}
      />
    )
    expect(screen.getByTestId('summary-drawdown')).toHaveTextContent('Max drawdown (net)')
    expect(screen.getByTestId('summary-drawdown')).toHaveTextContent('over 7 trades')
  })

  it('renders no placeholder card when the risk block is not sent', () => {
    const { container } = render(
      <RiskStrip consistency={null} risk={null} formatCurrency={money} />
    )
    expect(screen.queryByText(/Not sent by the server/)).not.toBeInTheDocument()
    expect(screen.queryByTestId('group-risk-missing')).not.toBeInTheDocument()
    expect(container.textContent ?? '').not.toMatch(/no capital basis|planned stop/i)
  })

  it('shows no risk-multiple, stop, Sharpe or setup wording in the statistics', () => {
    render(<DiscretionaryStatistics summary={summary()} formatCurrency={money} />)
    expect(document.body.textContent ?? '').not.toMatch(
      /R-multiple|stop adherence|slippage|Sharpe|Sortino|setup/i
    )
  })
})

describe('tile hints state their sample', () => {
  const s = (extra = {}) => {
    const base = summary(extra)
    return { ...base, metrics: { ...base.metrics, trades_count: 12 } }
  }

  it('names the completed trades behind a charges hint', () => {
    expect(
      chargesInsightHint(s({ costs: costsBlock({ charges_pct_of_gross_profit: 42.5 }) }), money)
    ).toContain('over 12 completed trades')
    expect(
      chargesInsightHint(s({ realized_gross: 50, realized_net_of_charges: -10 }), money)
    ).toContain('over 12 completed trades')
  })

  it('compares largest win and loss at any trade count and names the count', () => {
    const c = consistency()
    const big = s({
      costs: costsBlock({ charges_pct_of_gross_profit: 5 }),
      consistency: {
        ...c,
        largest_win: { ...c.largest_win!, net_pnl: 100 },
        largest_loss: { ...c.largest_loss!, net_pnl: -250 },
      },
    })
    expect(largestLossHint(big, money)).toContain('over 12 completed trades')
    const few = { ...big, metrics: { ...big.metrics, trades_count: 3 } }
    expect(largestLossHint(few, money)).toContain('larger than your largest winner')
    expect(largestLossHint(few, money)).toContain('over 3 completed trades')
  })
})

describe('the weekday tab needs two weekdays to compare, not a trade or day count', () => {
  const sample = (trading_days: number) => ({
    completed_trades: 40,
    trading_days,
    min_trades: 30,
    min_days: 10,
    sufficient: trading_days >= 10,
  })

  it('shows the entry-day weekday tab over 3 trading days, and not with one weekday', () => {
    const base = summary()
    const wk = [breakdownRow('Mon', { trades: 6 }), breakdownRow('Tue', { trades: 6 })]
    const mk = (days: number) => ({
      ...base,
      metrics: { ...base.metrics, trades_count: 12 },
      breakdowns: { time_of_day: [], weekday: wk, hold_bucket: [] },
      sample: sample(days),
    })
    expect(breakdownVisibility(mk(3)).patterns).toContain('weekday')
    const one = { ...mk(3), breakdowns: { time_of_day: [], weekday: [wk[0]], hold_bucket: [] } }
    expect(breakdownVisibility(one).patterns).not.toContain('weekday')
  })
})

describe('a weekday with no completed position shows dashes, not zero', () => {
  it('dashes the gross, charges and net cells of an empty row', () => {
    render(
      <CalendarTable
        rows={[
          calendarRow('Mon', {
            positions: 3,
            realized_gross: 300,
            charges: 30,
            net_of_charges: 270,
          }),
          calendarRow('Tue', {
            positions: 0,
            realized_gross: 0,
            charges: 0,
            net_of_charges: 0,
            win_rate: null,
          }),
        ]}
        first="Weekday"
        label={(r) => r.key}
        formatCurrency={money}
      />
    )
    const rows = screen.getAllByTestId('calendar-row')
    expect(rows[0]).toHaveTextContent('Rs 300.00')
    expect(rows[1]).not.toHaveTextContent('Rs 0.00')
    expect(rows[1].querySelectorAll('td')[3]).toHaveTextContent('—')
    expect(rows[1].querySelectorAll('td')[4]).toHaveTextContent('—')
    expect(rows[1].querySelectorAll('td')[5]).toHaveTextContent('—')
  })
})
