import type { EvaluationBlock, RunRecord } from '@/api/backtestResults'
import { fmtSigned, THIN_SAMPLE_TRADES } from '@/lib/backtestResults'

/** ok: satisfactory. partial: some checks run or minor caveats. problem: a check failed. unknown: not run or not recorded. */
export type ReliabilityState = 'ok' | 'partial' | 'problem' | 'unknown'

export interface ReliabilityIndicator {
  key: 'data' | 'sample' | 'statistics' | 'oos' | 'robustness' | 'execution'
  label: string
  state: ReliabilityState
  /** One short phrase saying why, from the run's own figures. */
  detail: string
}

export const RELIABILITY_STATE_LABEL: Record<ReliabilityState, string> = {
  ok: 'Satisfactory',
  partial: 'Partly checked',
  problem: 'Problem',
  unknown: 'Not checked',
}

const STATISTICS_DIMENSIONS = ['statistical_confidence']
const ROBUSTNESS_DIMENSIONS = ['robustness', 'regime_robustness', 'parameter_sensitivity']

/**
 * Rolls up the recorded gate statuses of some dimensions. No threshold is applied here: a failed
 * gate is a problem, a gate that did not run is not checked, and only passes with nothing left
 * unevaluated count as satisfactory. Report-only and not-applicable rows are ignored.
 */
export function dimensionState(
  evaluation: EvaluationBlock | null | undefined,
  names: string[]
): { state: ReliabilityState; detail: string } {
  const rows = (evaluation?.dimensions ?? [])
    .filter((d) => names.includes(d.name))
    .flatMap((d) => d.rows)
  if (rows.length === 0) return { state: 'unknown', detail: 'not recorded for this run' }
  const count = (status: string) => rows.filter((r) => r.status === status).length
  const fail = count('fail')
  const pass = count('pass')
  const notRun = count('not_evaluated')
  if (fail > 0) return { state: 'problem', detail: `${fail} failed` }
  if (pass === 0) return { state: 'unknown', detail: 'not run' }
  if (notRun > 0) return { state: 'partial', detail: `${pass} passed, ${notRun} not run` }
  return { state: 'ok', detail: `${pass} passed` }
}

/** The compact reliability read of a run, built only from fields the server already sends. */
export function reliabilityIndicators(run: RunRecord): ReliabilityIndicator[] {
  const coverage = run.session_coverage?.coverage_class
  const data: ReliabilityIndicator = !coverage
    ? { key: 'data', label: 'Data quality', state: 'unknown', detail: 'coverage not recorded' }
    : coverage === 'COMPLETE'
      ? { key: 'data', label: 'Data quality', state: 'ok', detail: 'complete coverage' }
      : coverage === 'MINOR_GAPS'
        ? { key: 'data', label: 'Data quality', state: 'partial', detail: 'minor gaps' }
        : {
            key: 'data',
            label: 'Data quality',
            state: 'problem',
            detail: coverage === 'MATERIAL_GAPS' ? 'material gaps' : 'severe gaps',
          }

  const trades = run.metrics.trades
  const sample: ReliabilityIndicator =
    trades === undefined
      ? { key: 'sample', label: 'Sample size', state: 'unknown', detail: 'not recorded' }
      : trades === 0
        ? { key: 'sample', label: 'Sample size', state: 'problem', detail: 'no trades' }
        : trades < THIN_SAMPLE_TRADES
          ? {
              key: 'sample',
              label: 'Sample size',
              state: 'partial',
              detail: `${trades} trades, thin`,
            }
          : { key: 'sample', label: 'Sample size', state: 'ok', detail: `${trades} trades` }

  const stats = dimensionState(run.evaluation, STATISTICS_DIMENSIONS)
  const robust = dimensionState(run.evaluation, ROBUSTNESS_DIMENSIONS)

  const oos = run.out_of_sample
  const oosInd: ReliabilityIndicator =
    !oos || !oos.trades
      ? { key: 'oos', label: 'Out-of-sample', state: 'unknown', detail: 'no held-out result' }
      : oos.net_bps > 0
        ? {
            key: 'oos',
            label: 'Out-of-sample',
            state: 'ok',
            detail: `${fmtSigned(oos.net_bps, 1)} bps net, ${oos.trades} trades`,
          }
        : {
            key: 'oos',
            label: 'Out-of-sample',
            state: 'problem',
            detail: `${fmtSigned(oos.net_bps, 1)} bps net, ${oos.trades} trades`,
          }

  const w = run.execution_warnings
  const guessed = w ? w.ambiguous_exit_trades + w.data_gap_trades : 0
  const execution: ReliabilityIndicator = !w
    ? { key: 'execution', label: 'Execution', state: 'unknown', detail: 'not recorded' }
    : guessed > 0
      ? {
          key: 'execution',
          label: 'Execution',
          state: 'partial',
          detail: `${guessed} trades rest on a guess about the bars`,
        }
      : { key: 'execution', label: 'Execution', state: 'ok', detail: 'no bar-level guesses' }

  return [
    data,
    sample,
    { key: 'statistics', label: 'Statistical tests', ...stats },
    oosInd,
    { key: 'robustness', label: 'Robustness', ...robust },
    execution,
  ]
}
