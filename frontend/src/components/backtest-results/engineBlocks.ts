import type {
  CoverageClass,
  EngineBlocks,
  ExecutionWarnings,
  LookaheadGuard,
  SessionCoverage,
} from '@/api/backtestResults'

export const NOT_RECORDED_FOR_RUN = 'not recorded for this run'

export const COVERAGE_LABEL: Record<CoverageClass, string> = {
  COMPLETE: 'Complete coverage',
  MINOR_GAPS: 'Minor gaps (up to 1% of sessions)',
  MATERIAL_GAPS: 'Material gaps (1% to 5%): results may be affected',
  SEVERE_GAPS: 'Severe gaps (over 5%): not reliable historical validation',
}

export const COVERAGE_TONE: Record<CoverageClass, string> = {
  COMPLETE: 'border-green-600/60 bg-green-600/10',
  MINOR_GAPS: 'border-amber-500/60 bg-amber-500/10',
  MATERIAL_GAPS: 'border-orange-500/60 bg-orange-500/10 text-orange-700',
  // the strongest styling used for a failure elsewhere on these pages
  SEVERE_GAPS: 'border-red-500/60 bg-red-500/10 text-red-700 font-semibold',
}

export interface RunBlocks {
  warnings: ExecutionWarnings | null
  guard: LookaheadGuard | null
  coverage: SessionCoverage | null
  costModelVersion: number | string | null
  engineVersion: number | string | null
}

const first = <T>(a: T | null | undefined, b: T | null | undefined): T | null => a ?? b ?? null

/** The engine 1.6 blocks of a run, preferring the run detail over the run record. Null when absent. */
export function runBlocks(run?: EngineBlocks | null, detail?: EngineBlocks | null): RunBlocks {
  return {
    warnings: first(detail?.execution_warnings, run?.execution_warnings),
    guard: first(detail?.lookahead_guard, run?.lookahead_guard),
    coverage: first(detail?.session_coverage, run?.session_coverage),
    costModelVersion: first(detail?.cost_model_version, run?.cost_model_version),
    engineVersion: first(detail?.engine_version, run?.engine_version),
  }
}

export const pct = (share: number | null | undefined, digits = 1): string =>
  typeof share === 'number' && Number.isFinite(share) ? `${(share * 100).toFixed(digits)}%` : 'n/a'

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/** The missing and expected session counts of a run as "N of M expected sessions (x%)", or null. */
export function missingOfExpected(c: SessionCoverage): string | null {
  const u = c.universe
  const expected = u.expected_sessions_after_exclusions_total
  if (typeof expected !== 'number' || expected <= 0) return null
  return `${u.missing_full_sessions_total} of ${expected} expected sessions (${pct(u.missing_full_sessions_total / expected, 2)})`
}

/** The symbols in each class as one phrase, or null when the record carries no counts. */
export function classCounts(c: SessionCoverage): string | null {
  const u = c.universe
  const parts: [string, number | undefined][] = [
    ['complete', u.symbols_complete],
    ['minor gaps', u.symbols_minor_gaps],
    ['material gaps', u.symbols_material_gaps],
    ['severe gaps', u.symbols_severe_gaps],
  ]
  if (parts.every(([, n]) => typeof n !== 'number')) return null
  return parts.map(([label, n]) => `${n ?? 0} ${label}`).join(', ')
}

/** One plain sentence on what the coverage class means for how far to trust the result. */
export function coverageSentence(c: SessionCoverage): string {
  const u = c.universe
  if (c.coverage_class === 'COMPLETE') {
    return 'Every symbol has every official session in its tested range, and no bars are missing inside sessions.'
  }
  const of = missingOfExpected(c)
  const counts = classCounts(c)
  const detail = `${of ?? `${plural(u.missing_full_sessions_total, 'whole session')} missing across ${plural(u.symbols, 'symbol')}`}${counts ? `; symbols: ${counts}` : ''}.`
  if (c.coverage_class === 'MINOR_GAPS') {
    return `A few sessions are missing (up to 1% of the expected sessions for the worst symbol): ${detail} Read the result with a little more caution.`
  }
  if (c.coverage_class === 'MATERIAL_GAPS') {
    return `Between 1% and 5% of the expected sessions are missing for the worst symbol: ${detail} The result may change with complete data.`
  }
  return `More than 5% of the expected sessions are missing for the worst symbol: ${detail} Treat the result as indicative only.`
}

/** One line on ambiguous exits and data gaps, or null when the run has neither. */
export function warningsNote(w: ExecutionWarnings | null | undefined): string | null {
  if (!w) return null
  const parts: string[] = []
  if (w.ambiguous_exit_trades > 0) {
    parts.push(
      `${w.ambiguous_exit_trades} of ${w.trades} trades (${pct(w.ambiguous_exit_share)}) touched both stop and target in one bar and were counted as stopped out`
    )
  }
  if (w.data_gap_trades > 0) {
    parts.push(
      `${w.data_gap_trades} of ${w.trades} trades (${pct(w.data_gap_share)}) were open while a bar was missing from the data`
    )
  }
  return parts.length > 0 ? `${parts.join('; ')}.` : null
}
