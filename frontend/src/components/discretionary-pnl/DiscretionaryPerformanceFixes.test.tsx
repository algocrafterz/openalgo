import { describe, expect, it } from 'vitest'
import { summarizeUnrealized, totalCard } from '@/lib/discretionaryPnlView'
import { consistency, riskBlock, summary } from '@/test/discretionaryFixtures'
import { render, screen } from '@/test/test-utils'
import { RiskStrip } from './DiscretionaryInsights'
import { DiscretionaryStatistics, KeyFigures } from './DiscretionaryStatistics'
import { DiscretionaryHeader } from './DiscretionarySummary'

const money = (v: number) => `Rs ${v.toFixed(2)}`

function withMetrics(extra: Record<string, unknown>) {
  const base = summary()
  return { ...base, metrics: { ...base.metrics, ...extra } }
}

describe('win rate card', () => {
  it('shows wins, losses, break-even and sample size, with no small-sample note', () => {
    const s = withMetrics({
      trades_count: 12,
      net_basis_trades: 12,
      winning_trades: 5,
      losing_trades: 6,
      win_rate: 41.7,
    })
    render(
      <DiscretionaryHeader
        summary={s}
        formatCurrency={money}
        chargesHint=""
        unrealized={summarizeUnrealized(s)}
        total={totalCard(s.realized_net_of_charges, summarizeUnrealized(s), 0)}
      />
    )
    expect(screen.getByTestId('win-rate-counts')).toHaveTextContent(
      '5 wins / 6 losses / 1 break-even of 12 trades'
    )
    expect(screen.queryByTestId('win-rate-small-sample')).not.toBeInTheDocument()
    expect(screen.getByTestId('win-rate-detail')).not.toHaveTextContent(/small sample|indicative/i)
  })

  it('has no small-sample note at 30 trades either', () => {
    const s = withMetrics({
      trades_count: 30,
      net_basis_trades: 30,
      winning_trades: 15,
      losing_trades: 15,
      win_rate: 50,
    })
    render(
      <DiscretionaryHeader
        summary={s}
        formatCurrency={money}
        chargesHint=""
        unrealized={summarizeUnrealized(s)}
        total={totalCard(s.realized_net_of_charges, summarizeUnrealized(s), 0)}
      />
    )
    expect(screen.queryByTestId('win-rate-small-sample')).not.toBeInTheDocument()
  })
})

describe('average win, average loss and payoff', () => {
  it('reads n/a, never Rs 0, when there is no losing trade', () => {
    const s = withMetrics({
      trades_count: 3,
      net_basis_trades: 3,
      winning_trades: 3,
      losing_trades: 0,
      avg_win: 100,
      avg_loss: 0, // the server's 0.0 for "no losses"
      profit_factor: null,
    })
    render(<DiscretionaryStatistics summary={s} formatCurrency={money} />)
    expect(screen.getByTestId('stat-avg-loss')).toHaveTextContent('n/a')
    expect(screen.getByTestId('stat-avg-loss')).not.toHaveTextContent('Rs 0')
    expect(screen.getByTestId('stat-payoff')).toHaveTextContent('n/a')
    expect(screen.getByTestId('stat-avg-win')).toHaveTextContent('Rs 100.00')
  })

  it('reads n/a for profit factor when every trade is break-even', () => {
    const s = withMetrics({
      trades_count: 2,
      net_basis_trades: 2,
      winning_trades: 0,
      losing_trades: 0,
      avg_win: 0,
      avg_loss: 0,
      profit_factor: 0,
      expectancy: 0,
    })
    render(
      <>
        <KeyFigures summary={s} formatCurrency={money} />
        <DiscretionaryStatistics summary={s} formatCurrency={money} />
      </>
    )
    expect(screen.getByText('Profit factor (net)').parentElement).toHaveTextContent('n/a')
    expect(screen.getByTestId('stat-avg-win')).toHaveTextContent('n/a')
    expect(screen.getByTestId('stat-avg-loss')).toHaveTextContent('n/a')
  })

  it('shows the payoff ratio with a win and a loss', () => {
    const s = withMetrics({
      trades_count: 4,
      net_basis_trades: 4,
      winning_trades: 2,
      losing_trades: 2,
      avg_win: 200,
      avg_loss: 100,
      profit_factor: 2,
    })
    render(<DiscretionaryStatistics summary={s} formatCurrency={money} />)
    expect(screen.getByTestId('stat-payoff')).toHaveTextContent('2.00')
  })
})

describe('risk strip behaviour figures', () => {
  it('labels winning days as net with the sample, and shows objective behaviour text', () => {
    const c = consistency({
      completed_trades: 12,
      net_days: {
        total_days: 3,
        winning_days: 1,
        losing_days: 2,
        flat_days: 0,
        winning_day_pct: 33.33,
        net_unavailable_days: 0,
      },
      trades_per_day: { days: 3, avg: 2, max: 3, max_day: '2026-11-20' },
      reentry_after_loss: {
        window_minutes: 15,
        losses_followed_by_entry: 4,
        reentries_within_window: 1,
        shortest_gap_minutes: 5,
      },
      quantity_by_symbol: [
        {
          symbol: 'SBIN',
          exchange: 'NSE',
          positions: 5,
          min_quantity: 10,
          median_quantity: 20,
          max_quantity: 40,
        },
      ],
    })
    render(
      <RiskStrip
        consistency={c}
        risk={riskBlock({
          max_drawdown: 320,
          max_drawdown_detail: {
            depth: 320,
            start_at: '2026-11-19 09:20:00',
            start_basis: 'first position opened',
            trough_at: '2026-11-19 09:50:00',
            recovered: false,
            duration_minutes: 90,
          },
        })}
        formatCurrency={money}
      />
    )
    const days = screen.getByTestId('risk-winning-days')
    expect(days).toHaveTextContent('Winning days (net)')
    expect(days).toHaveTextContent('33.3%')
    expect(days).toHaveTextContent('1 of 3 days')
    expect(days).not.toHaveTextContent(/small sample/i)
    expect(screen.getByTestId('behaviour-trades-per-day')).toHaveTextContent('2 avg, 3 max')
    expect(screen.getByTestId('behaviour-reentry')).toHaveTextContent(
      'Re-entered within 15 min after a loss'
    )
    expect(screen.getByTestId('behaviour-quantity')).toHaveTextContent('SBIN: 10 / 20 / 40')
    expect(screen.getByTestId('behaviour-drawdown-recovery')).toHaveTextContent(
      'Not recovered, 1 h 30 min'
    )
    expect(screen.getByTestId('behaviour-tiles')).toHaveTextContent('(12 completed trades)')
    expect(screen.getByTestId('behaviour-reentry')).toHaveTextContent('1 of 4')
    expect(screen.getByTestId('behaviour-tiles').textContent ?? '').not.toMatch(
      /revenge|fomo|tilt|panic/i
    )
  })

  it('draws no behaviour figure the server did not send, and no placeholder text', () => {
    render(
      <RiskStrip
        consistency={consistency({ completed_trades: 12 })}
        risk={riskBlock()}
        formatCurrency={money}
      />
    )
    for (const id of ['behaviour-trades-per-day', 'behaviour-reentry', 'behaviour-quantity']) {
      expect(screen.queryByTestId(id)).not.toBeInTheDocument()
    }
    expect(screen.queryByText(/insufficient data/)).not.toBeInTheDocument()
    // the gross day counts stay, labelled as gross
    expect(screen.getByTestId('risk-days')).toHaveTextContent('Profitable / losing days (gross)')
  })

  it('draws the behaviour block for 9 completed trades, with the count in its heading', () => {
    const quantity = [
      {
        symbol: 'SBIN',
        exchange: 'NSE',
        positions: 9,
        min_quantity: 10,
        median_quantity: 20,
        max_quantity: 40,
      },
    ]
    render(
      <RiskStrip
        consistency={consistency({ completed_trades: 9, quantity_by_symbol: quantity })}
        risk={riskBlock()}
        formatCurrency={money}
      />
    )
    expect(screen.getByTestId('behaviour-tiles')).toHaveTextContent('(9 completed trades)')
    expect(screen.getByTestId('behaviour-quantity')).toHaveTextContent('SBIN: 10 / 20 / 40')
  })

  it('draws no behaviour block when there is no completed trade', () => {
    render(
      <RiskStrip
        consistency={consistency({ completed_trades: 0 })}
        risk={riskBlock()}
        formatCurrency={money}
      />
    )
    expect(screen.queryByTestId('behaviour-tiles')).not.toBeInTheDocument()
  })

  it('shows trades per day over one day and re-entries after 2 losses, each with its own count', () => {
    render(
      <RiskStrip
        consistency={consistency({
          completed_trades: 12,
          trades_per_day: { days: 1, avg: 12, max: 12, max_day: '2026-11-20' },
          reentry_after_loss: {
            window_minutes: 15,
            losses_followed_by_entry: 2,
            reentries_within_window: 2,
            shortest_gap_minutes: 1,
          },
        })}
        risk={riskBlock()}
        formatCurrency={money}
      />
    )
    expect(screen.getByTestId('behaviour-trades-per-day')).toHaveTextContent('1 trading day')
    expect(screen.getByTestId('behaviour-reentry')).toHaveTextContent('2 of 2')
  })

  it('leaves out re-entry when no loss was followed by an entry', () => {
    render(
      <RiskStrip
        consistency={consistency({
          completed_trades: 12,
          reentry_after_loss: {
            window_minutes: 15,
            losses_followed_by_entry: 0,
            reentries_within_window: 0,
            shortest_gap_minutes: null,
          },
        })}
        risk={riskBlock()}
        formatCurrency={money}
      />
    )
    expect(screen.queryByTestId('behaviour-reentry')).not.toBeInTheDocument()
  })
})
