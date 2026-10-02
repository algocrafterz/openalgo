import { useState } from 'react'
import type { RunComparison, RunRef } from '@/api/backtestResults'
import { CompareCurves } from './CompareCurves'
import { CompareHeader, OverlapBanner, type PeriodView, VIEW_LABEL } from './CompareHeader'
import { ComparePerformance } from './ComparePerformance'
import { DiffSection, StrategySection } from './CompareSections'

/** Presentation of the backend's run comparison. Nothing here is computed or judged. */
export function CompareDetail(props: { comparison: RunComparison; runA: RunRef; runB: RunRef }) {
  // Keyed by the pair so the chosen period view never carries over to another comparison.
  return <CompareDetailInner key={`${props.runA.run_id}+${props.runB.run_id}`} {...props} />
}

/** The view actually shown: the chosen one when its data exists, otherwise the full period. */
export function effectiveView(view: PeriodView, comparison: RunComparison): PeriodView {
  if (view === 'common' && comparison.views.common) return 'common'
  if (view === 'incremental' && comparison.views.incremental) return 'incremental'
  return 'full'
}

function CompareDetailInner({
  comparison,
  runA,
  runB,
}: {
  comparison: RunComparison
  runA: RunRef
  runB: RunRef
}) {
  const [chosen, setView] = useState<PeriodView>('full')
  const view = effectiveView(chosen, comparison)
  const { overlap, views, strategy, dataset } = comparison
  return (
    <div className="space-y-8" data-testid="compare-detail">
      <CompareHeader
        runA={runA}
        runB={runB}
        versions={strategy.version}
        classes={comparison.change_classes}
      />
      <OverlapBanner overlap={overlap} views={views} value={view} onChange={setView} />
      <StrategySection strategy={strategy} />
      <DiffSection id="cmp-scope" title="Scope" rows={comparison.scope} />
      <DiffSection id="cmp-dataset" title="Dataset" rows={dataset.fields}>
        {dataset.fingerprint_changed && (
          <p className="rounded-md border p-2 text-sm">
            The dataset fingerprint changed: history was corrected or backfilled between these runs.
          </p>
        )}
      </DiffSection>
      <DiffSection id="cmp-execution" title="Execution" rows={comparison.execution} />
      <DiffSection id="cmp-cost" title="Cost" rows={comparison.cost} />
      <section aria-labelledby="cmp-performance" className="space-y-2">
        <h3 id="cmp-performance" className="font-medium">
          Performance
        </h3>
        <p className="text-sm" data-testid="performance-title">
          {VIEW_LABEL[view]}
        </p>
        <ComparePerformance comparison={comparison} view={view} />
      </section>
      <section aria-labelledby="cmp-curves" className="space-y-2">
        <h3 id="cmp-curves" className="font-medium">
          Curves
        </h3>
        <CompareCurves
          comparison={comparison}
          view={view}
          incrementalRun={views.incremental?.run ?? null}
        />
      </section>
    </div>
  )
}
