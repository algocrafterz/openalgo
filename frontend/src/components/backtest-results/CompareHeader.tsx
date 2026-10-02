import type { Overlap, PeriodViews, RunRef } from '@/api/backtestResults'
import { GLOSSARY_IDS } from '@/lib/backtestGlossary'
import { InfoTip } from './InfoTip'

export type PeriodView = 'full' | 'common' | 'incremental'

export function changeClassLabel(cls: string): string {
  return cls === 'RESEARCH_RERUN' ? 'Identical re-run' : cls
}

function RunBlock({ side, run, version }: { side: string; run: RunRef; version: string | null }) {
  return (
    <div className="text-sm">
      <div className="text-muted-foreground">{side}</div>
      <div className="font-mono">{run.run_id}</div>
      <div>
        {run.date}
        {version ? `, strategy ${version}` : ''}
      </div>
    </div>
  )
}

export function CompareHeader({
  runA,
  runB,
  versions,
  classes,
}: {
  runA: RunRef
  runB: RunRef
  versions: { a: string | null; b: string | null }
  classes: string[]
}) {
  return (
    <header className="space-y-3">
      <h3 className="font-medium">Run comparison</h3>
      <div className="flex flex-wrap gap-8">
        <RunBlock side="Run A" run={runA} version={versions.a} />
        <RunBlock side="Run B" run={runB} version={versions.b} />
      </div>
      <ul aria-label="Change classes" className="flex flex-wrap gap-2 text-xs">
        {classes.map((c, i) => {
          const id = c.toLowerCase()
          return (
            <li key={c} className="flex items-center gap-1 rounded-full border px-2 py-0.5">
              {i === 0 && <span className="font-medium">Primary:</span>}
              <span>{changeClassLabel(c)}</span>
              {(GLOSSARY_IDS as string[]).includes(id) && <InfoTip term={id} />}
            </li>
          )
        })}
      </ul>
    </header>
  )
}

export const VIEW_LABEL: Record<PeriodView, string> = {
  full: 'Full period',
  common: 'Common period',
  incremental: 'Incremental period',
}

const OPTIONS: { id: PeriodView; label: string }[] = (
  ['full', 'common', 'incremental'] as PeriodView[]
).map((id) => ({ id, label: VIEW_LABEL[id] }))

function reason(id: PeriodView, views: PeriodViews): string | null {
  if (id === 'common' && views.common === null) return 'No common sessions: the runs do not overlap'
  if (id === 'incremental' && views.incremental === null) {
    return 'No sessions outside the common period'
  }
  return null
}

export function OverlapBanner({
  overlap,
  views,
  value,
  onChange,
}: {
  overlap: Overlap
  views: PeriodViews
  value: PeriodView
  onChange: (v: PeriodView) => void
}) {
  return (
    <section aria-label="Period overlap" className="space-y-3">
      {overlap.warning && (
        <div role="alert" className="rounded-md border border-yellow-500/60 p-3 text-sm space-y-1">
          <div className="font-medium">Comparison warning</div>
          <div>{overlap.warning}</div>
          <div>This is not a pure strategy-version comparison.</div>
        </div>
      )}
      <fieldset className="space-y-1">
        <legend className="text-sm font-medium">Period view</legend>
        <div role="radiogroup" aria-label="Period view" className="flex flex-wrap gap-4">
          {OPTIONS.map((o) => {
            const why = reason(o.id, views)
            return (
              <label key={o.id} className="text-sm flex items-center gap-2">
                <input
                  type="radio"
                  name="period-view"
                  value={o.id}
                  checked={value === o.id}
                  disabled={why !== null}
                  onChange={() => onChange(o.id)}
                />
                {o.label}
                {why && <span className="text-xs text-muted-foreground">({why})</span>}
              </label>
            )
          })}
        </div>
      </fieldset>
    </section>
  )
}
