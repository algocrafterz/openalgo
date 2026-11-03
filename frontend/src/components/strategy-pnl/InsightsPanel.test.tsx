import { describe, expect, it } from 'vitest'
import type { PerformanceInsights } from '@/api/strategyPnl'
import { render, screen } from '@/test/test-utils'
import { GradeBadge, InsightsPanel } from './InsightsPanel'

const money = (v: number) => `Rs ${v.toFixed(0)}`

function insights(extra: Partial<PerformanceInsights> = {}): PerformanceInsights {
  return {
    concentration: {
      traded_days: 25,
      trades: 196,
      net: 13919,
      best_day: 6346,
      worst_day: -1040,
      mean_daily: 557,
      median_daily: 116,
      profitable_days: 16,
      profitable_days_pct: 64,
      avg_winning_day: 1159,
      avg_losing_day: -513,
      net_ex_best_day: 7573,
      net_ex_best_five_days: 425,
      top1_days_share_pct: 45.6,
      top3_days_share_pct: 79.8,
      top5_days_share_pct: 96.9,
      top10pct_trades: 20,
      top10pct_trades_share_of_net_pct: 88.8,
      by_strategy: {
        A: {
          trades: 112,
          net: 9232,
          share_of_net_pct: 66.3,
          best_day_net: 6346,
          best_day_share_of_own_pct: 68.7,
          net_ex_best_day: 2886,
          net_ex_portfolio_best_day: 2886,
        },
        B: {
          trades: 84,
          net: 4687,
          share_of_net_pct: 33.7,
          best_day_net: 2011,
          best_day_share_of_own_pct: 42.9,
          net_ex_best_day: 2676,
          net_ex_portfolio_best_day: 4687,
        },
      },
    },
    drawdown: {
      basis: 'realized',
      net: 13919,
      capital_base: 200000,
      return_on_capital_pct: 6.96,
      max_drawdown: -1504,
      max_drawdown_pct: -0.75,
      recovery_factor: 9.26,
      drawdown_duration_sessions: 4,
      drawdown_recovered: true,
      calmar_ratio: null,
      max_drawdown_trade_sequence: -1814,
      recovery_factor_trade_sequence: 7.67,
    },
    streaks: { max_consecutive_losses: 6, avg_losing_streak: 1.86, losing_streaks: 42 },
    gross_profit_factor: 2.04,
    net_profit_factor: 1.78,
    grading: {
      metrics: {
        net_profit_factor: { metric_value: 1.78, grade: 'good', reason: null },
        win_rate_pct: { metric_value: 60.2, grade: 'excellent', reason: null },
        sharpe: { metric_value: null, grade: null, reason: 'not available' },
        recovery_factor: {
          metric_value: 9.26,
          grade: null,
          reason: 'needs 60 trading sessions, has 28',
        },
      },
      overall: null,
      preliminary: null,
      assessment: 'none',
      overall_note: 'Not graded: recovery factor (needs 60 trading sessions, has 28)',
      sample: { trades: 196, sessions: 28 },
    },
    deployment: {
      avg_daily_peak_deployed: 261728,
      max_deployed: 813783,
      deployment_days: 25,
      deployment_excluded: 3,
      deployment_basis: 'entry value',
    },
    rolling: {
      combined: [
        { n: 20, date: '2026-10-01', exp20: 10, pf50: null },
        { n: 21, date: '2026-10-02', exp20: 20, pf50: null },
      ],
      by_strategy: { A: [], B: [] },
      expectancy_window: 20,
      pf_window: 50,
    },
    monthly: [
      {
        month: '2026-10',
        trades: 161,
        traded_days: 19,
        net: 13736,
        win_rate: 61.5,
        profit_factor: 1.99,
        profitable_days_pct: 68.4,
      },
    ],
    entry_quality: {
      by_hour: [],
      by_weekday: [],
      by_direction: [],
      by_session_part: [
        {
          label: 'First hour',
          trades: 50,
          win_rate: 58,
          net: 4788,
          profit_factor: 2.19,
          expectancy: 96,
          expectancy_r: 0.265,
          r_trades: 49,
        },
      ],
      entry_excluded: 3,
      first_hour_ends: '10:15',
    },
    slippage: {
      covered: 186,
      total: 196,
      basis: 'x',
      net: 290,
      adverse_total: 4901,
      favourable_total: -4611,
      adverse_trades: 87,
      favourable_trades: 88,
      net_bps: 0.4,
      adverse_bps: 6.3,
      by_strategy: {},
      by_symbol: [],
    },
    ...extra,
  }
}

describe('InsightsPanel', () => {
  it('shows concentration, withheld grades with their reasons, and the data caveats', () => {
    render(<InsightsPanel insights={insights()} formatCurrency={money} />)
    expect(screen.getByText('45.6%')).toBeInTheDocument()
    expect(screen.getByText('Profit without best day')).toBeInTheDocument()
    expect(screen.getByText(/Not graded: recovery factor/)).toBeInTheDocument()
    expect(screen.getByText('needs 60 trading sessions, has 28')).toBeInTheDocument()
    expect(screen.getByText(/3 trades with unreliable times are left out/)).toBeInTheDocument()
    expect(screen.getByText(/3 trades with no reliable entry time/)).toBeInTheDocument()
    expect(
      screen.getByText(/simulated, so it is not a measure of live execution/)
    ).toBeInTheDocument()
  })

  it('shows an overall grade only when the report has one', () => {
    const graded = insights()
    graded.grading = {
      ...graded.grading,
      overall: 'good',
      assessment: 'validated',
      overall_note: null,
    }
    render(<InsightsPanel insights={graded} formatCurrency={money} />)
    expect(screen.getByText(/weakest of the graded measures/)).toBeInTheDocument()
  })

  it('labels a preliminary grade as not validated and not proof of an edge', () => {
    const prelim = insights()
    prelim.grading = { ...prelim.grading, preliminary: 'good', assessment: 'preliminary' }
    render(<InsightsPanel insights={prelim} formatCurrency={money} />)
    expect(screen.getByText('Preliminary: Good')).toBeInTheDocument()
    expect(screen.getByText(/not evidence of a proven edge/)).toBeInTheDocument()
  })

  it('says so when no trade could be matched to a signal', () => {
    const none = insights()
    none.slippage = { ...none.slippage, covered: 0 }
    render(<InsightsPanel insights={none} formatCurrency={money} />)
    expect(screen.getByText(/entry slippage is not measured here/)).toBeInTheDocument()
  })
})

describe('GradeBadge', () => {
  it('labels each grade and a missing one', () => {
    const { rerender } = render(<GradeBadge grade="excellent" />)
    expect(screen.getByText('Excellent')).toBeInTheDocument()
    rerender(<GradeBadge grade="poor" />)
    expect(screen.getByText('Poor')).toBeInTheDocument()
    rerender(<GradeBadge grade={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
