import type { CoverageClass, ExecutionWarnings, SessionCoverage } from '@/api/backtestResults'
import { cn } from '@/lib/utils'
import { COVERAGE_LABEL, COVERAGE_TONE, coverageSentence, warningsNote } from './engineBlocks'
import { InfoTip } from './InfoTip'

/** The coverage class as a text chip. The label carries the meaning, colour only supports it. */
export function CoverageChip({
  coverageClass,
  className,
}: {
  coverageClass: CoverageClass
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium',
        COVERAGE_TONE[coverageClass],
        className
      )}
      data-testid="coverage-chip"
    >
      {COVERAGE_LABEL[coverageClass]}
    </span>
  )
}

/**
 * Level 1: how complete the data behind the result is, with one plain sentence, and a one-line
 * note when some trades rest on a guess about the bars. Reads "not recorded for this run" for a
 * run made before the engine wrote the coverage block.
 */
export function DataCoverageTrust({
  coverage,
  warnings,
}: {
  coverage: SessionCoverage | null
  warnings: ExecutionWarnings | null
}) {
  const note = warningsNote(warnings)
  return (
    <section
      className="max-w-5xl space-y-1 rounded-md border p-3 text-sm"
      data-testid="data-coverage-trust"
      aria-label="Data coverage"
    >
      <h2 className="text-sm font-semibold text-muted-foreground">
        Data coverage
        <InfoTip term="session_coverage" />
      </h2>
      {coverage ? (
        <p className="flex flex-wrap items-center gap-2">
          <CoverageChip coverageClass={coverage.coverage_class} />
          <span data-testid="coverage-sentence">{coverageSentence(coverage)}</span>
        </p>
      ) : (
        <p className="text-muted-foreground" data-testid="coverage-sentence">
          Data coverage: not recorded for this run.
        </p>
      )}
      {coverage?.disclosure &&
        (coverage.coverage_class === 'MATERIAL_GAPS' ||
          coverage.coverage_class === 'SEVERE_GAPS') && (
          <p
            role="note"
            className={cn(
              'rounded-md border px-2 py-1 font-medium',
              COVERAGE_TONE[coverage.coverage_class]
            )}
            data-testid="coverage-disclosure"
          >
            {coverage.disclosure}
          </p>
        )}
      {note && (
        <p data-testid="execution-warning-note">
          {note}
          <InfoTip term="ambiguous_exit" />
        </p>
      )}
    </section>
  )
}
