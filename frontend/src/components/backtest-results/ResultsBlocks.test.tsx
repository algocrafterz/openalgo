import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { EvaluationBlock, RegimesBlock } from '@/api/backtestResults'
import { EvaluationTable } from './EvaluationTable'
import { ExportMenu } from './ExportMenu'
import { RegimeTable } from './RegimeTable'
import { ResearchNotes } from './ResearchNotes'
import { StrategyDefinition } from './StrategyDefinition'

const EVALUATION: EvaluationBlock = {
  version: 1,
  dimensions: [
    {
      name: 'edge',
      rows: [
        {
          gate: 'G1.2',
          title: 'Net expectancy',
          status: 'pass',
          hard: true,
          value: 0.1275,
          threshold: '>= +0.05R',
          evidence: 'net +0.128R over 3572 trades',
        },
        {
          gate: 'G2.2',
          title: 'Significance',
          status: 'fail',
          hard: true,
          value: 1.2,
          threshold: 't >= 2.5',
          evidence: 't 1.2 at 1 trial',
        },
      ],
    },
    {
      name: 'regime_robustness',
      rows: [
        {
          gate: 'X.2',
          title: 'Regime dispersion',
          status: 'report',
          hard: false,
          value: null,
          threshold: '',
          evidence: 'too few sampled regimes',
        },
        {
          gate: 'G4.2',
          title: 'Latency',
          status: 'not_evaluated',
          hard: false,
          value: null,
          threshold: '',
          evidence: 'needs a delayed-entry rerun',
        },
      ],
    },
  ],
  summary: { counts: { pass: 1, fail: 1, report: 1, not_evaluated: 1 }, hard_failed: ['G2.2'] },
}

const REGIMES: RegimesBlock = {
  version: 1,
  min_trades: 30,
  unclassified_trades: 10,
  trend: [
    {
      regime: 'up',
      trades: 410,
      net_r: 0.4253,
      total_r: 174.4,
      win_rate: 0.22,
      max_dd_r: 99.6,
      low_sample: false,
    },
    {
      regime: 'range',
      trades: 12,
      net_r: -0.1,
      total_r: -1.2,
      win_rate: 0.2,
      max_dd_r: 3,
      low_sample: true,
    },
  ],
  vol: [
    {
      regime: 'high',
      trades: 2225,
      net_r: 0.1,
      total_r: 235.7,
      win_rate: 0.21,
      max_dd_r: 243,
      low_sample: false,
    },
  ],
}

describe('ExportMenu', () => {
  it('links the canonical JSON and the two CSV views for the run', () => {
    render(<ExportMenu ideaId="IDEA-023" runId="2026-09-30-gap-v4" />)
    const menu = screen.getByTestId('export-2026-09-30-gap-v4')
    const hrefs = within(menu)
      .getAllByRole('link')
      .map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual([
      '/api/backtest-results/IDEA-023/runs/2026-09-30-gap-v4/export?format=json',
      '/api/backtest-results/IDEA-023/runs/2026-09-30-gap-v4/export?format=metrics',
      '/api/backtest-results/IDEA-023/runs/2026-09-30-gap-v4/export?format=series',
    ])
  })

  it('offers trades only when a per-trade file is stored', () => {
    const { rerender } = render(<ExportMenu ideaId="IDEA-023" runId="r1" />)
    expect(screen.queryByText('Trades CSV')).not.toBeInTheDocument()
    rerender(<ExportMenu ideaId="IDEA-023" runId="r1" hasTrades />)
    expect(screen.getByText('Trades CSV')).toHaveAttribute(
      'href',
      '/api/backtest-results/IDEA-023/runs/r1/export?format=trades'
    )
  })

  it('encodes ids into the path', () => {
    render(<ExportMenu ideaId="IDEA-001" runId="a b" />)
    expect(screen.getAllByRole('link')[0].getAttribute('href')).toContain('/runs/a%20b/export')
  })
})

describe('EvaluationTable', () => {
  it('groups live gates by dimension and keeps not-evaluated ones listed, folded away', () => {
    render(<EvaluationTable evaluation={EVALUATION} />)
    expect(screen.getByTestId('dimension-edge')).toBeInTheDocument()
    expect(screen.getByTestId('dimension-regime_robustness')).toBeInTheDocument()
    expect(within(screen.getByTestId('gates-dead')).getByTestId('gate-G4.2')).toHaveTextContent(
      'not evaluated'
    )
    expect(within(screen.getByTestId('gate-G1.2')).getByText('pass')).toBeInTheDocument()
  })

  it('highlights a failed hard gate and lists it in the alert', () => {
    render(<EvaluationTable evaluation={EVALUATION} />)
    expect(screen.getByRole('alert')).toHaveTextContent('G2.2')
    expect(screen.getByTestId('gate-G2.2').className).toContain('bg-red-500/10')
    expect(screen.getByTestId('gate-G1.2').className).not.toContain('bg-red-500/10')
  })

  it('labels the regime-dispersion gate as a heuristic', () => {
    render(<EvaluationTable evaluation={EVALUATION} />)
    expect(within(screen.getByTestId('gate-X.2')).getByText(/heuristic/)).toBeInTheDocument()
    expect(within(screen.getByTestId('gate-G1.2')).queryByText(/heuristic/)).toBeNull()
  })

  it('never shows a composite score', () => {
    render(<EvaluationTable evaluation={EVALUATION} />)
    expect(screen.queryByText(/score/i)).not.toBeInTheDocument()
  })

  it('says so when no hard gate failed', () => {
    const clean = { ...EVALUATION, summary: { ...EVALUATION.summary, hard_failed: [] } }
    render(<EvaluationTable evaluation={clean} />)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('No hard gate has failed.')).toBeInTheDocument()
  })
})

describe('RegimeTable', () => {
  it('shows trend and volatility tables with the low-sample flag', () => {
    render(<RegimeTable regimes={REGIMES} />)
    expect(within(screen.getByTestId('regime-Trend-up')).getByText('410')).toBeInTheDocument()
    expect(within(screen.getByTestId('regime-Trend-up')).getByText('ok')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('regime-Trend-range')).getByText(/low sample/)
    ).toBeInTheDocument()
    expect(screen.getByTestId('regime-Volatility-high')).toBeInTheDocument()
  })

  it('mentions trades outside the classified history', () => {
    render(<RegimeTable regimes={REGIMES} />)
    expect(screen.getByText(/10 trades fell outside/)).toBeInTheDocument()
  })
})

describe('StrategyDefinition', () => {
  it('shows a clear state when there is no specification on file', () => {
    render(<StrategyDefinition spec={null} />)
    expect(screen.getByTestId('no-spec')).toHaveTextContent('No specification on file')
  })

  it('renders the rules from the spec', () => {
    render(
      <StrategyDefinition
        spec={{
          name: 'VWAP trend',
          direction: 'long_only',
          entry: ['Close above VWAP by 0.2 percent', 'Fill on the next open'],
          exit: ['Close below VWAP'],
          stop_loss: '1.5 ATR below entry',
          take_profit: 'none',
          parameters: { atr_mult: 1.5 },
          rationale: 'Trend persistence.',
        }}
      />
    )
    const panel = screen.getByTestId('strategy-definition')
    expect(panel).toHaveTextContent('VWAP trend')
    expect(panel).toHaveTextContent('Close above VWAP by 0.2 percent')
    expect(panel).toHaveTextContent('1.5 ATR below entry')
    expect(panel).toHaveTextContent('atr_mult = 1.5')
    expect(panel).toHaveTextContent('Trend persistence.')
  })
})

describe('ResearchNotes', () => {
  it('renders the notes apart from the definition', () => {
    render(<ResearchNotes notes={'## Decision log\n- 2026-09-30: rejected'} />)
    expect(screen.getByTestId('research-notes')).toHaveTextContent('Decision log')
    expect(screen.getByText(/not the current strategy definition/)).toBeInTheDocument()
  })

  it('renders nothing when there are no notes', () => {
    const { container } = render(<ResearchNotes notes="" />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('EvaluationTable collapsing', () => {
  const MIXED: EvaluationBlock = {
    version: 1,
    dimensions: [
      {
        name: 'integrity',
        rows: [
          {
            gate: 'G0.4',
            title: 'Data coverage',
            status: 'not_evaluated',
            hard: false,
            value: null,
            threshold: '',
            evidence: 'needs a coverage report',
          },
        ],
      },
      {
        name: 'edge',
        rows: [
          {
            gate: 'G1.2',
            title: 'Net expectancy',
            status: 'fail',
            hard: true,
            value: -1,
            threshold: '> 0',
            evidence: 'negative',
          },
          {
            gate: 'G6.1',
            title: 'Session',
            status: 'not_applicable',
            hard: false,
            value: null,
            threshold: '',
            evidence: 'swing',
          },
        ],
      },
    ],
    summary: { counts: { fail: 1, not_evaluated: 1, not_applicable: 1 }, hard_failed: ['G1.2'] },
  }

  it('shows decision rows in the tables and folds the rest into one expander', () => {
    render(<EvaluationTable evaluation={MIXED} />)
    expect(
      within(screen.getByTestId('dimension-edge')).getByTestId('gate-G1.2')
    ).toBeInTheDocument()
    expect(screen.queryByTestId('dimension-integrity')).not.toBeInTheDocument()
    const folded = screen.getByTestId('gates-dead')
    expect(folded).toHaveTextContent('2 gates not evaluated or not applicable')
    expect(within(folded).getByTestId('gate-G0.4')).toBeInTheDocument()
    expect(within(folded).getByTestId('gate-G6.1')).toBeInTheDocument()
  })
})
