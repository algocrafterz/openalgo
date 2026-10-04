import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { EvaluationBlock, GateRow, Promotion } from '@/api/backtestResults'
import { DecisionPanel } from './DecisionPanel'
import { EvaluationTable } from './EvaluationTable'
import { PromotionGate } from './PromotionGate'

vi.mock('./InfoTip', () => ({ InfoTip: () => null }))

function row(gate: string, status: GateRow['status'] = 'pass'): GateRow {
  return {
    gate,
    title: `Title ${gate}`,
    status,
    hard: false,
    value: 1,
    threshold: 'x',
    evidence: 'e',
  }
}

const GATES = ['G1.2', 'G2.1', 'G2.2', 'G3.1', 'G3.3', 'G3.4', 'G6.5', 'G6.4']

const EVALUATION: EvaluationBlock = {
  version: 1,
  dimensions: [{ name: 'edge', rows: GATES.map((g) => row(g)) }],
  summary: { counts: {}, hard_failed: [] },
}

const PROMOTION: Promotion = {
  stages: {
    backtest: {
      decision: 'NOT_READY',
      gates: GATES.map((g) => ({ gate: g, status: 'PASS' as const })),
      reasons: [],
    },
    paper: { decision: 'BLOCKED', gates: [], reasons: [] },
    live: { decision: 'BLOCKED', gates: [], reasons: [] },
  },
  next_stage: 'paper',
}

describe('PromotionGate show trades', () => {
  it('adds a button only to gates that map to a subset, with that subset', () => {
    const onShow = vi.fn()
    render(<PromotionGate promotion={PROMOTION} evaluation={EVALUATION} onShowTrades={onShow} />)
    for (const g of ['G1.2', 'G2.1', 'G3.1', 'G6.5']) {
      expect(
        within(screen.getByTestId(`promotion-gate-${g}`)).getByRole('button', {
          name: 'Show trades',
        })
      ).toBeInTheDocument()
    }
    for (const g of ['G2.2', 'G3.3', 'G3.4', 'G6.4']) {
      expect(
        within(screen.getByTestId(`promotion-gate-${g}`)).queryByRole('button', {
          name: 'Show trades',
        })
      ).not.toBeInTheDocument()
    }
    fireEvent.click(
      within(screen.getByTestId('promotion-gate-G3.1')).getByRole('button', { name: 'Show trades' })
    )
    expect(onShow).toHaveBeenCalledWith('oos')
    fireEvent.click(
      within(screen.getByTestId('promotion-gate-G6.5')).getByRole('button', { name: 'Show trades' })
    )
    expect(onShow).toHaveBeenLastCalledWith('gap_exits')
    fireEvent.click(
      within(screen.getByTestId('promotion-gate-G1.2')).getByRole('button', { name: 'Show trades' })
    )
    expect(onShow).toHaveBeenLastCalledWith('all')
  })

  it('has no buttons without a handler', () => {
    render(<PromotionGate promotion={PROMOTION} evaluation={EVALUATION} />)
    expect(screen.queryByRole('button', { name: 'Show trades' })).not.toBeInTheDocument()
  })
})

describe('EvaluationTable show trades', () => {
  it('adds the same mapped buttons to gate rows', () => {
    const onShow = vi.fn()
    render(<EvaluationTable evaluation={EVALUATION} onShowTrades={onShow} />)
    expect(screen.getAllByRole('button', { name: /Show trades/ })).toHaveLength(4)
    fireEvent.click(
      within(screen.getByTestId('gate-G3.1')).getByRole('button', { name: /Show trades/ })
    )
    expect(onShow).toHaveBeenCalledWith('oos')
    expect(within(screen.getByTestId('gate-G2.2')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('has no buttons without a handler', () => {
    render(<EvaluationTable evaluation={EVALUATION} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('DecisionPanel show trades', () => {
  it('keeps the gate link and adds the button for a mapped reason', () => {
    const onShow = vi.fn()
    render(
      <DecisionPanel
        verdict={{
          run_verdict: 'NO_EDGE',
          primary_failed_gate: null,
          caps_applied: [],
          reasons: [
            { text: 'OOS weak', gate: 'G3.1', status: 'FAIL', evidence_ref: 'evaluation:G3.1' },
            { text: 'Sig low', gate: 'G2.2', status: 'FAIL', evidence_ref: 'evaluation:G2.2' },
          ],
        }}
        promotion={null}
        nextAction={null}
        captured
        evaluation={EVALUATION}
        onShowTrades={onShow}
      />
    )
    expect(screen.getByRole('link', { name: 'Gate G3.1' })).toHaveAttribute('href', '#gate-G3.1')
    const buttons = screen.getAllByRole('button', { name: 'Show trades' })
    expect(buttons).toHaveLength(1)
    fireEvent.click(buttons[0])
    expect(onShow).toHaveBeenCalledWith('oos')
  })
})
