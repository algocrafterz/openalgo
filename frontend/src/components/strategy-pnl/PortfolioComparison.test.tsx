import { describe, expect, it } from 'vitest'
import type { StrategyComparisonResponse } from '@/api/strategyPnl'
import { render, screen } from '@/test/test-utils'
import { PortfolioComparison } from './PortfolioComparison'

const money = (v: number) => `Rs ${v.toFixed(0)}`

const data: StrategyComparisonResponse = {
  status: 'success',
  portfolio: {
    net: -100,
    net_profit_factor: 0.7,
    return_on_capital_pct: -0.05,
    max_drawdown: -150,
    max_drawdown_pct: -0.07,
    recovery_factor: null,
    overall_grade: null,
    preliminary_grade: 'good',
    overall_note: 'Not graded.',
  },
  correlation: {
    strategies: ['A', 'B'],
    matrix: [
      [1, null],
      [null, 1],
    ],
    sessions: 5,
  },
  contribution: [
    {
      strategy: 'A',
      net: -60,
      share_of_net_pct: null,
      portfolio_dd_with: -150,
      portfolio_dd_without: -90,
      drawdown_change: -60,
    },
    {
      strategy: 'B',
      net: -40,
      share_of_net_pct: null,
      portfolio_dd_with: -150,
      portfolio_dd_without: -110,
      drawdown_change: -40,
    },
  ],
}

describe('PortfolioComparison', () => {
  it('shows a plain dash, not a dash with a percent sign, when a share is unknown', () => {
    render(<PortfolioComparison data={data} formatCurrency={money} />)
    expect(screen.queryByText('—%')).not.toBeInTheDocument()
    expect(screen.getAllByText('Larger by Rs 60').length).toBe(1)
  })

  it('renders nothing without a portfolio block', () => {
    const { container } = render(
      <PortfolioComparison data={{ status: 'success' }} formatCurrency={money} />
    )
    expect(container).toBeEmptyDOMElement()
  })
})
