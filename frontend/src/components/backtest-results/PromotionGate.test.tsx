import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { EvaluationBlock, GateRow, Promotion } from '@/api/backtestResults'
import { PromotionGate } from './PromotionGate'

vi.mock('./InfoTip', () => ({
  InfoTip: ({ term }: { term: string }) => <span>{term}</span>,
}))

function row(gate: string, status: GateRow['status'], over: Partial<GateRow> = {}): GateRow {
  return {
    gate,
    title: `Title ${gate}`,
    status,
    hard: true,
    value: 1.5,
    threshold: '>= 2.0',
    evidence: `evidence for ${gate}`,
    ...over,
  }
}

const EVALUATION: EvaluationBlock = {
  version: 1,
  dimensions: [
    { name: 'edge', rows: [row('G1.2', 'pass'), row('G2.2', 'fail')] },
    {
      name: 'forward',
      rows: [
        row('G7.1', 'not_evaluated', { value: null }),
        row('G9.1', 'not_evaluated', { value: null }),
      ],
    },
  ],
  summary: { counts: {}, hard_failed: ['G2.2'] },
}

const PROMOTION: Promotion = {
  stages: {
    backtest: {
      decision: 'NOT_READY',
      gates: [
        { gate: 'G1.2', status: 'PASS' },
        { gate: 'G2.2', status: 'FAIL' },
      ],
      reasons: ['G2.2 failed'],
    },
    paper: { decision: 'BLOCKED', gates: [{ gate: 'G7.1', status: 'NOT_RUN' }], reasons: [] },
    live: { decision: 'BLOCKED', gates: [{ gate: 'G9.1', status: 'NOT_RUN' }], reasons: [] },
  },
  next_stage: null,
}

describe('PromotionGate', () => {
  it('groups gates by stage with decision and counts', () => {
    render(<PromotionGate promotion={PROMOTION} evaluation={EVALUATION} />)
    const bt = screen.getByTestId('gate-stage-backtest')
    expect(bt).toHaveTextContent('Backtest')
    expect(bt).toHaveTextContent('Not ready')
    expect(bt).toHaveTextContent('1 pass')
    expect(bt).toHaveTextContent('1 fail')
    expect(screen.getByTestId('gate-stage-paper')).toHaveTextContent('1 not run')
    expect(screen.getByTestId('gate-stage-live')).toHaveTextContent('Blocked')
  })

  it('expands a failed item to its evaluation row', () => {
    render(<PromotionGate promotion={PROMOTION} evaluation={EVALUATION} />)
    const item = screen.getByTestId('promotion-gate-G2.2')
    expect(within(item).queryByText('evidence for G2.2')).not.toBeInTheDocument()
    fireEvent.click(within(item).getByRole('button'))
    expect(within(item).getAllByText('Title G2.2')).toHaveLength(2)
    expect(within(item).getByText('evidence for G2.2')).toBeInTheDocument()
    expect(within(item).getByText(/>= 2\.0/)).toBeInTheDocument()
    expect(within(item).getByText(/1\.5/)).toBeInTheDocument()
  })

  it('shows Not run distinctly from pass', () => {
    render(<PromotionGate promotion={PROMOTION} evaluation={EVALUATION} />)
    const notRun = screen.getByTestId('promotion-gate-G7.1')
    const pass = screen.getByTestId('promotion-gate-G1.2')
    expect(notRun).toHaveTextContent('Not run')
    expect(notRun.querySelector('[data-status="NOT_RUN"]')).not.toBeNull()
    expect(pass.querySelector('[data-status="PASS"]')).not.toBeNull()
    const cls = (el: Element | null) => el?.className ?? ''
    expect(cls(notRun.querySelector('[data-status="NOT_RUN"]'))).not.toBe(
      cls(pass.querySelector('[data-status="PASS"]'))
    )
    expect(cls(notRun.querySelector('[data-status="NOT_RUN"]'))).not.toMatch(/green/)
  })

  it('has no expander for a passing item', () => {
    render(<PromotionGate promotion={PROMOTION} evaluation={EVALUATION} />)
    expect(within(screen.getByTestId('promotion-gate-G1.2')).queryByRole('button')).toBeNull()
  })

  it('renders nothing when there is no promotion', () => {
    const { container } = render(<PromotionGate promotion={null} evaluation={EVALUATION} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('survives a missing evaluation row', () => {
    render(<PromotionGate promotion={PROMOTION} evaluation={null} />)
    fireEvent.click(within(screen.getByTestId('promotion-gate-G2.2')).getByRole('button'))
    expect(screen.getByTestId('promotion-gate-G2.2')).toHaveTextContent('No evaluation row')
  })
})

describe('PromotionGate robustness', () => {
  it('survives a missing stage, missing gates and unknown decision and status', () => {
    const odd = {
      stages: {
        backtest: { decision: 'NEW_DECISION', gates: [{ gate: 'G1.2', status: 'WEIRD' }] },
        live: { decision: 'BLOCKED' },
      },
      next_stage: 'paper',
    } as unknown as Promotion
    render(<PromotionGate promotion={odd} evaluation={EVALUATION} />)
    const backtest = screen.getByTestId('gate-stage-backtest')
    expect(backtest).toHaveTextContent('NEW_DECISION')
    expect(within(backtest).getByTestId('promotion-gate-G1.2')).toHaveTextContent('WEIRD')
    expect(screen.getByTestId('gate-stage-paper')).toBeInTheDocument()
    expect(screen.getByTestId('gate-stage-live')).toHaveTextContent('Blocked')
  })

  it('does not collide keys when a gate id repeats', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const dup = {
      stages: {
        backtest: {
          decision: 'NOT_READY',
          gates: [
            { gate: 'G1.2', status: 'PASS' },
            { gate: 'G1.2', status: 'FAIL' },
          ],
          reasons: [],
        },
        paper: { decision: 'BLOCKED', gates: [], reasons: [] },
        live: { decision: 'BLOCKED', gates: [], reasons: [] },
      },
      next_stage: 'paper',
    } as unknown as Promotion
    render(<PromotionGate promotion={dup} evaluation={EVALUATION} />)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
