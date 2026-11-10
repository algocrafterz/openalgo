import { describe, expect, it } from 'vitest'
import type {
  EvaluationBlock,
  GateCorrection,
  GateRow,
  GateStatus,
  Promotion,
} from '@/api/backtestResults'
import { readinessGateIds, validationChecks } from './validationChecks'

const row = (gate: string, status: GateStatus, over: Partial<GateRow> = {}): GateRow => ({
  gate,
  title: `Title ${gate}`,
  status,
  hard: false,
  value: null,
  threshold: '',
  evidence: '',
  ...over,
})

const evaluation = (rows: GateRow[]): EvaluationBlock => ({
  version: 1,
  dimensions: [{ name: 'all', rows }],
  summary: { counts: {}, hard_failed: [] },
})

const PROMOTION: Promotion = {
  stages: {
    backtest: { decision: 'NOT_READY', gates: [], reasons: [] },
    paper: {
      decision: 'BLOCKED',
      gates: [
        { gate: 'G7.1', status: 'NOT_RUN' },
        { gate: 'G6.6', status: 'NOT_RUN' },
        { gate: 'G4.8', status: 'NOT_RUN' },
      ],
      reasons: [],
    },
    live: { decision: 'BLOCKED', gates: [{ gate: 'G9.1', status: 'NOT_RUN' }], reasons: [] },
  },
  next_stage: null,
}

const byId = (groups: ReturnType<typeof validationChecks>) =>
  Object.fromEntries(groups.map((g) => [g.id, g]))

const EXEMPT_PROMOTION: Promotion = {
  ...PROMOTION,
  stages: {
    ...PROMOTION.stages,
    live: {
      decision: 'BLOCKED',
      reasons: [],
      // The paper-exempt gates are listed under live, but stay in the backtest verdict.
      gates: [
        { gate: 'G0.7', status: 'NOT_RUN' },
        { gate: 'G2.4', status: 'NOT_RUN' },
        { gate: 'G9.1', status: 'NOT_RUN' },
      ],
    },
  },
}

describe('paper-exempt gates', () => {
  it('stay backtest checks even though the promotion record lists them under live', () => {
    expect([...readinessGateIds(EXEMPT_PROMOTION)].sort()).toEqual(['G4.8', 'G6.6', 'G7.1', 'G9.1'])
    const g = byId(
      validationChecks(
        evaluation([
          row('G0.7', 'not_evaluated'),
          row('G2.4', 'not_evaluated'),
          row('G9.1', 'not_evaluated'),
        ]),
        [],
        EXEMPT_PROMOTION
      )
    )
    expect(g.data.rows.map((r) => r.gate)).toEqual(['G0.7'])
    expect(g.sample.rows.map((r) => r.gate)).toEqual(['G2.4'])
    expect(Object.values(g).flatMap((x) => x.rows.map((r) => r.gate))).not.toContain('G9.1')
  })

  it('reads a run sealed before the override as recorded: G4.8 and G6.6 stay backtest checks', () => {
    const sealed: Promotion = {
      ...PROMOTION,
      stages: {
        ...PROMOTION.stages,
        paper: { decision: 'BLOCKED', reasons: [], gates: [{ gate: 'G7.1', status: 'NOT_RUN' }] },
      },
    }
    const g = byId(
      validationChecks(
        evaluation([row('G4.8', 'not_evaluated'), row('G6.6', 'not_evaluated')]),
        [],
        sealed
      )
    )
    expect(g.robustness.rows.map((r) => r.gate)).toEqual(['G4.8'])
    expect(g.execution.rows.map((r) => r.gate)).toEqual(['G6.6'])
  })
})

describe('readinessGateIds', () => {
  it('is every gate the backend files under the paper or live stage', () => {
    expect([...readinessGateIds(PROMOTION)].sort()).toEqual(['G4.8', 'G6.6', 'G7.1', 'G9.1'])
    expect(readinessGateIds(null).size).toBe(0)
  })
})

describe('validationChecks', () => {
  it('leaves paper and live gates out of the backtest checks, even when their class is deployment', () => {
    const groups = validationChecks(
      evaluation([
        row('G1.2', 'pass'),
        row('G7.1', 'not_evaluated'),
        row('G6.6', 'not_evaluated'),
        row('G4.8', 'not_evaluated'),
        row('G9.1', 'not_evaluated'),
      ]),
      [],
      PROMOTION
    )
    const all = groups.flatMap((g) => g.rows.map((r) => r.gate))
    expect(all).toEqual(['G1.2'])
  })

  it('keeps a deployment-class gate that the backend files under the backtest stage', () => {
    // G6.9 (the backtest respects the square-off) is a hard gate of the backtest stage.
    const groups = byId(
      validationChecks(
        evaluation([row('G6.9', 'fail', { hard: true }), row('G6.1', 'pass')]),
        [],
        PROMOTION
      )
    )
    expect(groups.execution.state).toBe('failed')
    expect(groups.execution.decidesVerdict).toBe(true)
    expect(groups.execution.rows.map((r) => r.gate)).toEqual(['G6.9', 'G6.1'])
  })

  it('groups gates into the questions a trader asks, in reading order', () => {
    const groups = validationChecks(
      evaluation([
        row('G0.1', 'pass'),
        row('G1.2', 'pass'),
        row('G2.1', 'pass'),
        row('G3.2', 'pass'),
        row('G4.1', 'pass'),
        row('G5.1', 'pass'),
        row('G5.3', 'pass'),
        row('X.4', 'report'),
      ]),
      [],
      PROMOTION
    )
    expect(groups.map((g) => g.id)).toEqual([
      'edge',
      'random',
      'oos',
      'robustness',
      'sample',
      'execution',
      'data',
    ])
    const g = byId(groups)
    expect(g.robustness.rows.map((r) => r.gate)).toEqual(['G4.1', 'G5.1'])
    expect(g.execution.rows.map((r) => r.gate)).toEqual(['G5.3'])
    expect(g.random.rows.map((r) => r.gate)).toEqual(['X.4'])
  })

  it('takes the worst recorded status and never counts a gate that was not run as a pass', () => {
    const g = byId(
      validationChecks(
        evaluation([
          row('G3.1', 'pass'),
          row('G3.2', 'not_evaluated', { title: 'Out-of-sample result' }),
          row('G1.1', 'pass'),
          row('G1.2', 'fail', { hard: true, title: 'Net expectancy' }),
          row('G1.3', 'not_evaluated'),
          row('G2.1', 'pass'),
          row('G2.2', 'pass'),
          row('G4.2', 'report'),
        ]),
        [],
        PROMOTION
      )
    )
    expect(g.oos.state).toBe('not_run')
    expect(g.oos.summary).toContain('1 not run: Out-of-sample result')
    expect(g.oos.summary).toContain('not a pass')
    expect(g.edge.state).toBe('failed')
    expect(g.edge.summary).toContain('1 failed: Net expectancy')
    expect(g.edge.summary).toContain('1 not run')
    expect(g.edge.decidesVerdict).toBe(true)
    expect(g.sample.state).toBe('passed')
    expect(g.sample.summary).toBe('2 checks passed.')
    expect(g.robustness.state).toBe('reported')
  })

  it('does not call a failed gate verdict-deciding unless the gate is hard', () => {
    const g = byId(
      validationChecks(evaluation([row('G3.2', 'fail', { hard: false })]), [], PROMOTION)
    )
    expect(g.oos.state).toBe('failed')
    expect(g.oos.decidesVerdict).toBe(false)
  })

  it('reads a gate the way an evaluator correction re-scored it', () => {
    const correction: GateCorrection = {
      gate: 'G3.2',
      recorded: { status: 'pass', value: null, threshold: '', evidence: '' },
      corrected: { status: 'fail', value: null, threshold: '', evidence: 'corrected' },
      changed_status: true,
      file: 'x',
      evaluated_ist: null,
      reason: null,
    }
    const g = byId(validationChecks(evaluation([row('G3.2', 'pass')]), [correction], PROMOTION))
    expect(g.oos.state).toBe('failed')
  })

  it('shows a gate that no group claims under Other checks instead of dropping it', () => {
    const groups = validationChecks(evaluation([row('G11.1', 'fail')]), [], PROMOTION)
    expect(groups.map((g) => g.id)).toEqual(['other'])
    expect(groups[0].rows.map((r) => r.gate)).toEqual(['G11.1'])
  })

  it('returns no groups when there is no evaluation', () => {
    expect(validationChecks(null, undefined, PROMOTION)).toEqual([])
  })

  it('keeps every backtest gate when the run has no promotion block', () => {
    const groups = validationChecks(evaluation([row('G7.1', 'not_evaluated')]), [], null)
    // Without stage information nothing can be filed as readiness, so nothing is hidden.
    expect(groups.flatMap((g) => g.rows.map((r) => r.gate))).toEqual(['G7.1'])
  })
})

describe('validationChecks random-entry comparison', () => {
  const note = 'Random entries did better: 17.76% against 14.67% per trade.'

  it('shows the recorded comparison when the run has no gate row for it', () => {
    const groups = validationChecks(
      evaluation([row('G1.2', 'pass'), row('G3.2', 'pass')]),
      [],
      PROMOTION,
      note
    )
    expect(groups.map((g) => g.id)).toEqual(['edge', 'random', 'oos'])
    const random = groups[1]
    expect(random.state).toBe('reported')
    expect(random.summary).toBe(note)
    expect(random.decidesVerdict).toBe(false)
  })

  it('prefers the gate row when the run has one, and adds nothing without a recorded comparison', () => {
    const withGate = validationChecks(evaluation([row('X.4', 'report')]), [], PROMOTION, note)
    expect(withGate.filter((g) => g.id === 'random')).toHaveLength(1)
    expect(withGate[0].summary).not.toBe(note)
    const none = validationChecks(evaluation([row('G1.2', 'pass')]), [], PROMOTION, null)
    expect(none.map((g) => g.id)).toEqual(['edge'])
  })
})
