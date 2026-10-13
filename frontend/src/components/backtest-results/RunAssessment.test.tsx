import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type {
  DerivedVerdict,
  EvaluationBlock,
  GateRow,
  Promotion,
  RunDetail,
  RunVerdictValue,
  TuningSummary,
} from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import { RunAssessment } from './RunAssessment'

function row(gate: string, status: GateRow['status'], extra: Partial<GateRow> = {}): GateRow {
  return {
    gate,
    title: `title ${gate}`,
    status,
    hard: true,
    value: null,
    threshold: '',
    evidence: '',
    ...extra,
  }
}

function evaluation(rows: GateRow[]): EvaluationBlock {
  return {
    version: 1,
    dimensions: [{ name: 'all', rows }],
    summary: { counts: {}, hard_failed: [] },
  }
}

function verdict(v: RunVerdictValue, qualifications: string[] = []): DerivedVerdict {
  return {
    run_verdict: v,
    primary_failed_gate: null,
    reasons: [],
    caps_applied: [],
    qualifications,
  }
}

function promotion(backtestGates: string[]): Promotion {
  const st = (decision: 'READY_FOR_NEXT_STAGE' | 'NOT_READY' | 'BLOCKED') => ({
    decision,
    gates: [],
    reasons: [],
  })
  return {
    stages: {
      backtest: {
        decision: 'READY_FOR_NEXT_STAGE',
        gates: backtestGates.map((gate) => ({ gate, status: 'PASS' as const })),
        reasons: [],
      },
      paper: st('READY_FOR_NEXT_STAGE'),
      live: st('BLOCKED'),
    },
    next_stage: 'paper',
  }
}

function detail(over: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: '2026-09-30-x-v1',
    schema: 3,
    captured: true,
    strategy_version: 'v1',
    parent_version: null,
    branch_label: null,
    change_classes: [],
    verdict: verdict('MARGINAL'),
    promotion: promotion(['G1.2', 'G2.1', 'G2.2', 'G3.2']),
    next_action: null,
    spec: null,
    fingerprints: null,
    scope: null,
    reproducibility: null,
    assumptions: null,
    missing_capabilities: [],
    manifest_sha256: null,
    created_ist: null,
    validity: { ok: true, failures: [], checks_run: ['a', 'b', 'c'] },
    data_quality: null,
    trials: null,
    ledgers: null,
    cost_components: null,
    has_trades: false,
    has_ledgers: false,
    ...over,
  }
}

const TUNED: TuningSummary = {
  tuning_id: '2026-10-13-001',
  combinations: 4,
  usable: 3,
  in_sample_end: '2024-03-28',
  held_out_from: '2024-04-01',
  selected: {
    values: { or_minutes: 15 },
    trades: 40,
    net_r: 0.4,
    region_worst_net_r: 0.3,
    region_median_net_r: 0.35,
    region_size: 3,
    region_possible: 3,
  },
  best_single: {
    values: { or_minutes: 5 },
    trades: 40,
    net_r: 0.9,
    region_worst_net_r: -0.2,
    region_median_net_r: 0.3,
    region_size: 2,
    region_possible: 2,
  },
  selected_is_not_the_single_best: true,
  pbo: { value: 0.25, role: 'diagnostic' },
}

describe('RunAssessment', () => {
  it('a promising candidate: passes shown, live-only requirements counted, not listed as gaps', () => {
    const run = makeRun({
      metrics: { ...makeRun().metrics, net_bps: 4.2, gross_bps: 12.0, expectancy_r: 0.12 },
      evaluation: evaluation([
        row('G1.2', 'pass', { value: 0.12, threshold: '>= 0.05' }),
        row('G2.2', 'pass'),
        row('G7.1', 'not_evaluated'),
        row('G8.2', 'not_evaluated'),
        row('G6.3', 'not_applicable'),
      ]),
    })
    render(
      <RunAssessment
        run={run}
        detail={detail({ verdict: verdict('MARGINAL', ['Universe is survivor-biased']) })}
      />
    )
    expect(screen.getByTestId('assessment-validity')).toHaveAttribute('data-state', 'valid')
    expect(screen.queryByTestId('assessment-shortfalls')).toBeNull()
    expect(screen.queryByTestId('assessment-missing')).toBeNull()
    expect(screen.getByTestId('assessment-worked')).toHaveTextContent(
      '2 checks passed, none failed at this stage'
    )
    expect(screen.getByTestId('assessment-other-counts')).toHaveTextContent(
      '2 required only at the paper or live stage; 1 do not apply'
    )
    expect(screen.getByTestId('assessment-limitations')).toHaveTextContent(
      'Universe is survivor-biased'
    )
    expect(screen.getByTestId('run-assessment')).toHaveTextContent(
      'Eligibility for paper trading is not approval for live trading'
    )
  })

  it('a NO_EDGE strategy: the failed gate shows observed against needed, with a linked follow-up', () => {
    const run = makeRun({
      evaluation: evaluation([
        row('G1.2', 'fail', { value: -0.02, threshold: '>= 0.05' }),
        row('G1.3', 'fail', { value: -0.1, threshold: '>= 0' }),
      ]),
    })
    render(<RunAssessment run={run} detail={detail({ verdict: verdict('NO_EDGE') })} />)
    const short = screen.getByTestId('assessment-shortfalls')
    expect(short).toHaveTextContent('G1.2 title G1.2: observed -0.020, needs >= 0.05')
    const recs = screen.getByTestId('assessment-recommendations')
    // One recommendation per family, naming its evidence.
    expect(within(recs).getAllByRole('listitem')).toHaveLength(1)
    expect(recs).toHaveTextContent('Investigate cost sensitivity')
    expect(recs).toHaveTextContent(
      'Evidence: G1.2 title G1.2: observed -0.020, needs >= 0.05 (failed)'
    )
  })

  it('an invalid run: validity failures are named and missing evidence is not a failure', () => {
    const run = makeRun({
      evaluation: evaluation([row('G2.1', 'not_evaluated', { threshold: '>= 200 trades' })]),
    })
    render(
      <RunAssessment
        run={run}
        detail={detail({
          verdict: verdict('INCONCLUSIVE'),
          validity: {
            ok: false,
            failures: [{ check: 'tuning_separation', detail: 'overlap' }],
            checks_run: ['tuning_separation'],
          },
        })}
      />
    )
    const v = screen.getByTestId('assessment-validity')
    expect(v).toHaveAttribute('data-state', 'invalid')
    expect(v).toHaveTextContent('tuning_separation: overlap')
    expect(screen.getByTestId('assessment-missing')).toHaveTextContent('G2.1')
    expect(screen.queryByTestId('assessment-shortfalls')).toBeNull()
    expect(screen.getByTestId('assessment-recommendations')).toHaveTextContent(
      'Collect more observations'
    )
  })

  it('missing metrics read n/a, never zero, and the cost line is withheld', () => {
    const run = makeRun({
      metrics: { trades: 0, gross_bps: 0, net_bps: 0 },
      in_sample: undefined,
      out_of_sample: undefined,
    })
    render(<RunAssessment run={run} detail={detail({ validity: null })} />)
    const m = screen.getByTestId('assessment-metrics')
    expect(m).toHaveTextContent('n/a / n/a')
    expect(m).not.toHaveTextContent('+0.00')
    expect(m).not.toHaveTextContent('0.0R')
    expect(screen.queryByTestId('assessment-costs')).toBeNull()
    expect(screen.getByTestId('assessment-validity')).toHaveAttribute('data-state', 'not_recorded')
  })

  it('strong headline returns with weak out-of-sample and costs: weaknesses are shown', () => {
    const run = makeRun({
      metrics: {
        ...makeRun().metrics,
        trades: 150,
        gross_bps: 30,
        net_bps: 18,
        expectancy_r: 0.5,
      },
      in_sample: { trades: 100, gross_bps: 40, net_bps: 30 },
      out_of_sample: { trades: 50, gross_bps: 2, net_bps: -6 },
      split: { type: 'chronological', oos_start: '2025-01-01' },
      evaluation: evaluation([
        row('G1.2', 'pass'),
        row('G3.2', 'fail', { value: -6, threshold: 'same sign as in-sample' }),
        row('G1.3', 'fail', { value: -0.4, threshold: '>= 0', hard: false }),
      ]),
    })
    render(<RunAssessment run={run} detail={detail()} />)
    expect(screen.getByTestId('assessment-metrics')).toHaveTextContent('+30.00 / -6.00')
    expect(screen.getByTestId('assessment-costs')).toHaveTextContent(
      'Costs take 12.0 bps of a 30.0 bps gross result per trade.'
    )
    expect(screen.getByTestId('assessment-shortfalls')).toHaveTextContent('(soft)')
    expect(screen.getByTestId('assessment-limitations')).toHaveTextContent(
      'Thin sample: 150 trades'
    )
    const recs = screen.getByTestId('assessment-recommendations')
    expect(recs).toHaveTextContent('Examine the out-of-sample result')
    expect(recs).toHaveTextContent('Do not re-tune on this hold-out')
  })

  it('costs that erase a gross edge are said plainly', () => {
    render(<RunAssessment run={makeRun()} detail={detail()} />)
    expect(screen.getByTestId('assessment-costs')).toHaveTextContent(
      'which removes the whole gross edge'
    )
  })

  it('without tuning it says tuning was not performed', () => {
    render(<RunAssessment run={makeRun()} detail={detail()} tuning={[]} />)
    expect(screen.getByTestId('tuning-none')).toHaveTextContent('Not performed')
  })

  it('with tuning it explains the neighbourhood, the rejected spike and PBO as a diagnostic', () => {
    render(<RunAssessment run={makeRun()} detail={detail()} tuning={[TUNED]} />)
    const t = screen.getByTestId('tuning-record')
    expect(t).toHaveTextContent(
      '4 settings tried (3 with enough trades) on sessions up to 2024-03-28'
    )
    expect(screen.getByTestId('tuning-stability')).toHaveAttribute('data-stability', 'stable')
    expect(t).toHaveTextContent('The single best setting (or_minutes=5, 0.900 R) was not chosen')
    expect(screen.getByTestId('tuning-run-match')).toHaveTextContent(
      'This run uses the selected values'
    )
    expect(t).toHaveTextContent('Diagnostic only, not a gate')
  })

  it('a selection with a losing neighbour is called fragile', () => {
    const fragile: TuningSummary = {
      ...TUNED,
      selected: {
        ...(TUNED.selected as NonNullable<TuningSummary['selected']>),
        region_worst_net_r: -0.1,
      },
    }
    render(
      <RunAssessment
        run={makeRun({ params: { or_minutes: 30 } })}
        detail={detail()}
        tuning={[fragile]}
      />
    )
    expect(screen.getByTestId('tuning-stability')).toHaveAttribute('data-stability', 'fragile')
    expect(screen.getByTestId('tuning-run-match')).toHaveTextContent(
      'does not use the selected values'
    )
  })
})
