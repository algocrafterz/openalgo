import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type {
  DerivedVerdict,
  EvaluationBlock,
  GateRow,
  NextAction,
  Promotion,
  RunVerdictValue,
} from '@/api/backtestResults'
import { DecisionPanel } from './DecisionPanel'
import { EvaluationTable } from './EvaluationTable'

vi.mock('./InfoTip', () => ({
  InfoTip: ({ term }: { term: string }) => <span data-testid="infotip">{term}</span>,
}))

function verdict(v: RunVerdictValue, extra: Partial<DerivedVerdict> = {}): DerivedVerdict {
  return {
    run_verdict: v,
    primary_failed_gate: null,
    reasons: [
      {
        text: 'Net expectancy is positive',
        gate: 'G1.2',
        status: 'PASS',
        evidence_ref: 'evaluation:G1.2',
      },
      {
        text: 'Significance is below the bar',
        gate: 'G2.2',
        status: 'FAIL',
        evidence_ref: 'evaluation:G2.2',
      },
    ],
    caps_applied: [],
    ...extra,
  }
}

function stage(decision: 'READY_FOR_NEXT_STAGE' | 'NOT_READY' | 'BLOCKED', reasons: string[] = []) {
  return { decision, gates: [], reasons }
}

function promotion(
  b = stage('NOT_READY', ['G2.2 failed']),
  p = stage('BLOCKED', ['Backtest not ready']),
  l = stage('BLOCKED')
): Promotion {
  return { stages: { backtest: b, paper: p, live: l }, next_stage: 'paper' }
}

const NEXT: NextAction = {
  action: 'Fix the significance gate',
  reason: 'Only 120 trades',
  gate: 'G2.2',
}

function renderPanel(over: Partial<React.ComponentProps<typeof DecisionPanel>> = {}) {
  return render(
    <DecisionPanel
      verdict={verdict('EDGE')}
      promotion={promotion()}
      nextAction={NEXT}
      captured={true}
      {...over}
    />
  )
}

describe('DecisionPanel verdict', () => {
  const cases: [RunVerdictValue, string][] = [
    ['EDGE', 'Edge'],
    ['MARGINAL', 'Marginal'],
    ['NO_EDGE', 'No edge'],
    ['INCONCLUSIVE', 'Inconclusive'],
  ]
  for (const [value, label] of cases) {
    it(`renders ${value} with its reasons`, () => {
      renderPanel({ verdict: verdict(value) })
      const region = screen.getByTestId('run-verdict')
      expect(
        within(region).getByText('Run verdict (calculated from the evidence)')
      ).toBeInTheDocument()
      expect(within(region).getByTestId('verdict-badge')).toHaveTextContent(label)
      expect(within(region).getByText('Net expectancy is positive')).toBeInTheDocument()
      expect(within(region).getByText('Significance is below the bar')).toBeInTheDocument()
    })
  }

  it('links a reason to its gate row', () => {
    renderPanel()
    const link = screen.getByRole('link', { name: /G2\.2/ })
    expect(link).toHaveAttribute('href', '#gate-G2.2')
  })

  it('shows caps_applied as held at marginal', () => {
    renderPanel({
      verdict: verdict('MARGINAL', { caps_applied: ['engine readiness: not reproducible'] }),
    })
    expect(screen.getByTestId('verdict-caps')).toHaveTextContent(
      'Held at MARGINAL: engine readiness: not reproducible'
    )
  })

  it('shows no verdict message when verdict is null on a captured run', () => {
    renderPanel({ verdict: null })
    expect(screen.getByTestId('run-verdict')).toHaveTextContent('No verdict')
  })
})

describe('DecisionPanel research state', () => {
  it('keeps state and verdict in separate labelled regions', () => {
    renderPanel({ researchState: 'promising', stage: 'backtest' })
    const state = screen.getByTestId('research-state')
    const run = screen.getByTestId('run-verdict')
    expect(state).not.toContainElement(run)
    expect(state).toHaveTextContent('Research state (set by hand)')
    expect(state).toHaveTextContent('Promising')
    expect(state).toHaveTextContent('Backtest')
    expect(run).not.toHaveTextContent('Promising')
    expect(run).not.toHaveTextContent('Research state')
  })

  it('never shows promising or robust as a verdict', () => {
    renderPanel({ researchState: 'promising' })
    expect(screen.getByTestId('verdict-badge')).not.toHaveTextContent(/promising|robust/i)
  })
})

describe('DecisionPanel promotion', () => {
  it('uses the stage wording for each decision', () => {
    renderPanel({
      promotion: promotion(
        stage('READY_FOR_NEXT_STAGE'),
        stage('NOT_READY', ['Few paper days']),
        stage('BLOCKED')
      ),
    })
    expect(screen.getByTestId('promotion-backtest')).toHaveTextContent('Ready for the next stage')
    expect(screen.getByTestId('promotion-paper')).toHaveTextContent('Not ready')
    expect(screen.getByTestId('promotion-paper')).toHaveTextContent('Few paper days')
    expect(screen.getByTestId('promotion-live')).toHaveTextContent('Blocked')
  })

  it('reads ready for the next stage for an EDGE run with later stages to run', () => {
    renderPanel({ promotion: promotion(stage('READY_FOR_NEXT_STAGE')) })
    expect(screen.getByTestId('promotion-backtest')).toHaveTextContent('Ready for the next stage')
  })

  it('renders the next action and why', () => {
    renderPanel()
    const region = screen.getByTestId('next-action')
    expect(region).toHaveTextContent('Fix the significance gate')
    expect(region).toHaveTextContent('Only 120 trades')
  })
})

describe('DecisionPanel legacy run', () => {
  it('shows the legacy note and verdict but no promotion block', () => {
    renderPanel({
      captured: false,
      verdict: null,
      promotion: null,
      nextAction: null,
      legacyVerdict: 'marginal',
      isLatestRun: true,
    })
    expect(screen.getByTestId('legacy-note')).toHaveTextContent(
      'This run predates calculated verdicts: verdict, promotion and next action were not captured'
    )
    expect(screen.getByTestId('legacy-verdict')).toHaveTextContent(/marginal/i)
    expect(screen.queryByTestId('promotion-backtest')).not.toBeInTheDocument()
    expect(screen.queryByTestId('next-action')).not.toBeInTheDocument()
  })

  it('omits the legacy verdict when there is none', () => {
    renderPanel({ captured: false, verdict: null, promotion: null, nextAction: null })
    expect(screen.getByTestId('legacy-note')).toBeInTheDocument()
    expect(screen.queryByTestId('legacy-verdict')).not.toBeInTheDocument()
  })

  it('expands a reason on click', () => {
    renderPanel()
    const btn = screen.getAllByRole('button', { name: /Significance is below the bar/ })[0]
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('DecisionPanel legacy verdict provenance', () => {
  const legacy = {
    captured: false,
    verdict: null,
    promotion: null,
    nextAction: null,
  } as const

  it('shows the selected run own recorded verdict, never the idea latest one', () => {
    renderPanel({ ...legacy, legacyVerdict: 'edge', runVerdict: 'NO_EDGE', isLatestRun: false })
    const region = screen.getByTestId('legacy-verdict')
    expect(region).toHaveTextContent(/no edge/i)
    expect(region).not.toHaveTextContent('Latest run verdict')
  })

  it('says no verdict was recorded for an older run without one', () => {
    renderPanel({ ...legacy, legacyVerdict: 'edge', isLatestRun: false })
    expect(screen.queryByTestId('legacy-verdict')).not.toBeInTheDocument()
    expect(screen.getByTestId('decision-panel')).toHaveTextContent(
      'No calculated verdict was recorded for this run'
    )
    expect(screen.getByTestId('decision-panel')).not.toHaveTextContent(/edge/i)
  })

  it('uses the idea verdict only for the latest run, labelled as such', () => {
    renderPanel({ ...legacy, legacyVerdict: 'marginal', isLatestRun: true })
    const region = screen.getByTestId('legacy-verdict')
    expect(region).toHaveTextContent('Latest run verdict')
    expect(region).toHaveTextContent(/marginal/i)
  })

  it('does not claim a recorded verdict when none exists for the latest run', () => {
    renderPanel({ ...legacy, legacyVerdict: 'none', isLatestRun: true })
    expect(screen.queryByTestId('legacy-verdict')).not.toBeInTheDocument()
  })
})

describe('DecisionPanel robustness', () => {
  it('survives a missing stage, missing reasons and an unknown decision', () => {
    const odd = {
      stages: {
        backtest: { decision: 'SOMETHING_NEW', gates: [] },
        live: { decision: 'BLOCKED', gates: [], reasons: [] },
      },
      next_stage: 'paper',
    } as unknown as Promotion
    renderPanel({ promotion: odd })
    expect(screen.getByTestId('promotion-backtest')).toHaveTextContent('SOMETHING_NEW')
    expect(screen.getByTestId('promotion-paper')).toBeInTheDocument()
    expect(screen.getByTestId('promotion-live')).toHaveTextContent('Blocked')
  })

  it('does not collide keys for reasons with the same text and reference', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const dup = {
      text: 'Same text',
      gate: null,
      status: null,
      evidence_ref: 'evaluation:G1.2',
    }
    renderPanel({
      verdict: verdict('EDGE', { reasons: [dup, { ...dup }] }),
      promotion: promotion(stage('NOT_READY', ['same', 'same'])),
    })
    expect(screen.getAllByText('Same text')).toHaveLength(2)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})

describe('DecisionPanel gate anchors', () => {
  function gateRow(gate: string, status: GateRow['status']): GateRow {
    return {
      gate,
      title: `Title ${gate}`,
      status,
      hard: false,
      value: null,
      threshold: '',
      evidence: 'ev',
    }
  }
  const EVALUATION: EvaluationBlock = {
    version: 1,
    dimensions: [
      { name: 'edge', rows: [gateRow('G2.1', 'pass'), gateRow('G7.1', 'not_evaluated')] },
    ],
    summary: { counts: {}, hard_failed: [] },
  }

  it('resolves a group id to the first row of that group', () => {
    renderPanel({
      evaluation: EVALUATION,
      verdict: verdict('EDGE', {
        reasons: [{ text: 'Group reason', gate: 'G2', status: 'PASS', evidence_ref: null }],
      }),
    })
    expect(screen.getByRole('link', { name: /G2/ })).toHaveAttribute('href', '#gate-G2.1')
  })

  it('opens the closed details holding the row when the link is clicked', async () => {
    render(
      <>
        <DecisionPanel
          verdict={verdict('EDGE', {
            reasons: [
              { text: 'Forward not run', gate: 'G7', status: 'NOT_RUN', evidence_ref: null },
            ],
          })}
          promotion={null}
          nextAction={null}
          captured={true}
          evaluation={EVALUATION}
        />
        <EvaluationTable evaluation={EVALUATION} />
      </>
    )
    const details = screen.getByTestId('gates-dead') as HTMLDetailsElement
    expect(details.open).toBe(false)
    fireEvent.click(screen.getByRole('link', { name: /G7/ }))
    await waitFor(() => expect(details.open).toBe(true))
    expect(within(details).getByTestId('gate-G7.1')).toBeInTheDocument()
  })

  it('opens the details when the page loads with a matching hash', async () => {
    window.location.hash = '#gate-G7.1'
    render(<EvaluationTable evaluation={EVALUATION} />)
    const details = screen.getByTestId('gates-dead') as HTMLDetailsElement
    await waitFor(() => expect(details.open).toBe(true))
    window.location.hash = ''
  })
})
