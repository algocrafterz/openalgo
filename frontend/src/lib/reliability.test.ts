import { describe, expect, it } from 'vitest'
import type { EvaluationBlock, GateRow, GateStatus } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import { dimensionState, reliabilityIndicators } from './reliability'

const row = (gate: string, status: GateStatus): GateRow => ({
  gate,
  title: gate,
  status,
  hard: false,
  value: null,
  threshold: '',
  evidence: '',
})

const evaluation = (dims: Record<string, GateStatus[]>): EvaluationBlock => ({
  version: 1,
  dimensions: Object.entries(dims).map(([name, statuses]) => ({
    name,
    rows: statuses.map((s, i) => row(`G${i}`, s)),
  })),
  summary: { counts: {}, hard_failed: [] },
})

const state = (run: ReturnType<typeof makeRun>, key: string) =>
  reliabilityIndicators(run).find((i) => i.key === key)

describe('dimensionState rolls up recorded gate statuses without a threshold', () => {
  const ev = (statuses: GateStatus[]) => evaluation({ statistical_confidence: statuses })
  const names = ['statistical_confidence']

  it('a failed gate is a problem even beside passes', () => {
    expect(dimensionState(ev(['pass', 'fail', 'pass']), names).state).toBe('problem')
  })
  it('passes with nothing left unevaluated are satisfactory', () => {
    expect(dimensionState(ev(['pass', 'pass', 'report']), names).state).toBe('ok')
  })
  it('passes with a gate not run are only partly checked', () => {
    const r = dimensionState(ev(['pass', 'not_evaluated']), names)
    expect(r.state).toBe('partial')
    expect(r.detail).toBe('1 passed, 1 not run')
  })
  it('a dimension that never ran reads not checked, never satisfactory', () => {
    expect(dimensionState(ev(['not_evaluated', 'not_evaluated']), names).state).toBe('unknown')
  })
  it('report-only and not-applicable rows do not count as a pass', () => {
    expect(dimensionState(ev(['report', 'not_applicable']), names).state).toBe('unknown')
  })
  it('a missing dimension or evaluation is not recorded, not satisfactory', () => {
    expect(dimensionState(undefined, names).state).toBe('unknown')
    expect(dimensionState(evaluation({ other: ['pass'] }), names).state).toBe('unknown')
  })
})

describe('reliabilityIndicators', () => {
  it('reads an old record with no recorded checks as not checked, except the sample', () => {
    const items = reliabilityIndicators(makeRun())
    expect(items.map((i) => i.key)).toEqual([
      'data',
      'sample',
      'statistics',
      'oos',
      'robustness',
      'execution',
    ])
    expect(items.filter((i) => i.state === 'unknown').map((i) => i.key)).toEqual([
      'data',
      'statistics',
      'oos',
      'robustness',
      'execution',
    ])
    expect(state(makeRun(), 'sample')?.state).toBe('ok')
  })

  it('a profitable run with 12 trades is a thin sample, not satisfactory', () => {
    const run = makeRun({ metrics: { trades: 12, gross_bps: 50, net_bps: 40 } })
    expect(state(run, 'sample')).toMatchObject({ state: 'partial', detail: '12 trades, thin' })
  })

  it('a run with no trades has a problem sample', () => {
    expect(
      state(makeRun({ metrics: { trades: 0, gross_bps: 0, net_bps: 0 } }), 'sample')?.state
    ).toBe('problem')
  })

  it.each([
    ['COMPLETE', 'ok'],
    ['MINOR_GAPS', 'partial'],
    ['MATERIAL_GAPS', 'problem'],
    ['SEVERE_GAPS', 'problem'],
  ] as const)('coverage %s is %s', (cls, expected) => {
    const run = makeRun({
      session_coverage: { coverage_class: cls } as never,
    })
    expect(state(run, 'data')?.state).toBe(expected)
  })

  it('a losing out-of-sample result is a problem and a missing one is not checked', () => {
    const split = { trades: 300, gross_bps: 2, net_bps: -4 }
    expect(state(makeRun({ out_of_sample: split }), 'oos')?.state).toBe('problem')
    expect(state(makeRun({ out_of_sample: { ...split, net_bps: 3 } }), 'oos')?.state).toBe('ok')
    expect(state(makeRun({ out_of_sample: { ...split, trades: 0 } }), 'oos')?.state).toBe('unknown')
  })

  it('counts trades resting on a guess about the bars as a caveat', () => {
    const w = (a: number, g: number) =>
      makeRun({
        execution_warnings: {
          trades: 100,
          ambiguous_exit_trades: a,
          ambiguous_exit_share: a / 100,
          data_gap_trades: g,
          data_gap_share: g / 100,
          entries_skipped_missing_bar: null,
        },
      })
    expect(state(w(0, 0), 'execution')?.state).toBe('ok')
    expect(state(w(5, 3), 'execution')).toMatchObject({
      state: 'partial',
      detail: '8 trades rest on a guess about the bars',
    })
  })
})
